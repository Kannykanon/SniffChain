"""Validates the simulator against honeypot.is on real, recent Uniswap V2 pairs on Base.

Base has far more live honeypots than Arc, so it's where the "sell blocked" path gets tested on real
scams. Both tools are pointed at the same pair.

    python scripts/validate_base.py [pairs, default 60]                 # scanner verdict (default)
    python scripts/validate_base.py [pairs] --single-size               # one 0.001 WETH round trip

Scanner mode runs exactly what users get on that pair: every quote size, static checks, and the
report.assess verdict; flagged = HIGH. Results: data/validation_base_scanner.csv (single-size mode:
data/validation_base.csv).

Resumable: pairs are cached and each result is appended as it lands, so rerunning continues.
"""
import csv
import json
import sys
import time
from pathlib import Path

import requests
from eth_abi import decode, encode
from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import config  # noqa: E402

config.select("base")

from scanner import report, static  # noqa: E402
from scanner.chain import V2Pair, checksum, token_info, w3  # noqa: E402
from scanner.simulate import round_trip  # noqa: E402

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AGGREGATE3 = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]
WETH = next(q for q in config.QUOTES if q.symbol == "WETH")
FACTORY, FEE_BPS = config.V2_FACTORIES[0]
MIN_DEPTH = 10**15      # 0.001 WETH; newer pairs often have no liquidity yet
SIZE = 0.001            # single-size mode: WETH per buy, small so shallow scam pairs can still fill
SCANNER_MODE = "--single-size" not in sys.argv


def sel(sig: str) -> bytes:
    return Web3.keccak(text=sig)[:4]


DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / ("validation_base_scanner.csv" if SCANNER_MODE else "validation_base.csv")


def multicall(calls: list[tuple[str, bytes]]) -> list[bytes | None]:
    """Batch read-only calls through Multicall3's aggregate3 (failures come back as None)."""
    out = []
    for i in range(0, len(calls), 300):
        batch = [(checksum(to), True, data) for to, data in calls[i:i + 300]]
        raw = w3().eth.call({"to": checksum(MULTICALL3), "data": AGGREGATE3 + encode(
            ["(address,bool,bytes)[]"], [batch])})
        out += [data if ok else None for ok, data in decode(["(bool,bytes)[]"], raw)[0]]
    return out


def recent_weth_pairs(want: int) -> list[tuple[str, str, int]]:
    """Newest pairs first, straight from factory.allPairs(i): no log scanning, a few batched calls."""
    total = decode(["uint256"], w3().eth.call({"to": checksum(FACTORY), "data": sel("allPairsLength()")}))[0]
    found, top = [], total
    while len(found) < want and total - top < 20_000:
        idx = range(top - 1, max(top - 500, 0) - 1, -1)
        pairs = [checksum(decode(["address"], r)[0]) for r in
                 multicall([(FACTORY, sel("allPairs(uint256)") + encode(["uint256"], [i])) for i in idx]) if r]
        info = multicall([c for p in pairs for c in (
            (p, sel("token0()")), (p, sel("token1()")),
            (WETH.address, sel("balanceOf(address)") + encode(["address"], [p])))])
        for n, p in enumerate(pairs):
            t0, t1, bal = info[3 * n:3 * n + 3]
            if not (t0 and t1 and bal):
                continue
            t0, t1 = (decode(["address"], t)[0].lower() for t in (t0, t1))
            depth = decode(["uint256"], bal)[0]
            if WETH.address in (t0, t1) and depth >= MIN_DEPTH:
                found.append((t1 if t0 == WETH.address else t0, p, depth))
        print(f"  pairs #{idx[-1]:,}-#{idx[0]:,}: {len(found)} WETH pairs with liquidity so far", file=sys.stderr)
        top = idx[-1]
    return found[:want]


def honeypot_is(token: str, pair: str) -> dict:
    for attempt in range(4):
        r = requests.get("https://api.honeypot.is/v2/IsHoneypot",
                         params={"address": token, "pair": pair, "chainID": config.CHAIN_ID}, timeout=30)
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("honeypot.is kept rate limiting")


FIELDS = ["token", "pair", "depth_weth", "ours_flag", "ours_verdict", "ours_reasons",
          "ours_buy_ok", "ours_sell_ok", "ours_loss", "ours_error",
          "hp_flag", "hp_success", "hp_is_honeypot", "hp_buy_tax", "hp_sell_tax", "hp_reason"]
PAIRS_CACHE = DATA / "validation_base_pairs.json"


def load_pairs(want: int) -> list[tuple[str, str, int]]:
    """Collected pairs are cached so an interrupted run never has to collect again."""
    if PAIRS_CACHE.exists():
        cached = [tuple(x) for x in json.loads(PAIRS_CACHE.read_text())]
        if len(cached) >= want:
            print(f"using {want} of {len(cached)} cached pairs", file=sys.stderr)
            return cached[:want]
    print(f"collecting {want} recent Uniswap V2 WETH pairs on Base...", file=sys.stderr)
    pairs = recent_weth_pairs(want)
    PAIRS_CACHE.parent.mkdir(exist_ok=True)
    PAIRS_CACHE.write_text(json.dumps(pairs))
    return pairs


def load_done() -> list[dict]:
    """Rows already in the CSV. Normalises an older column order so appends stay aligned."""
    if not OUT.exists():
        return []
    with OUT.open(newline="") as f:
        reader = csv.DictReader(f)
        rows, header = list(reader), reader.fieldnames
    if header != FIELDS:
        with OUT.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return rows


def check_pair(token: str, pair: str, depth: int) -> dict:
    row = {"token": checksum(token), "pair": pair, "depth_weth": depth / 1e18}
    if SCANNER_MODE:
        return row | scanner_verdict(token, V2Pair(pair, WETH, FEE_BPS, depth)) | honeypot_row(token, pair)
    try:
        rt = round_trip(V2Pair(pair, WETH, FEE_BPS, depth), token, SIZE)
        row |= {"ours_buy_ok": rt.buy_ok, "ours_sell_ok": rt.sell_ok, "ours_loss": round(rt.round_trip_loss, 4),
                "ours_error": rt.error or ""}
        # Our HIGH threshold: sell fails, or a round trip loses more than half.
        row["ours_flag"] = (rt.buy_ok and not rt.sell_ok) or (rt.sell_ok and rt.round_trip_loss > 0.5)
    except Exception as e:
        row |= {"ours_error": f"simulation error: {str(e)[:120]}", "ours_flag": None}
    return row | honeypot_row(token, pair)


def scanner_verdict(token: str, market: V2Pair) -> dict:
    """The scanner's own verdict on this exact pair: all sizes, static checks, report.assess."""
    try:
        trips = [round_trip(market, token, size) for size in market.quote.sizes]
        r = report.assess(report.ScanReport(token_info(token), static.analyse_token(token), market, None, trips))
    except Exception as e:
        return {"ours_error": f"scan error: {str(e)[:120]}", "ours_flag": None}
    return {"ours_flag": r.verdict == report.HIGH, "ours_verdict": r.verdict,
            "ours_reasons": " | ".join(r.reasons)[:300],
            "ours_error": next((t.error for t in trips if t.error), "") or ""}


def honeypot_row(token: str, pair: str) -> dict:
    row = {}
    try:
        h = honeypot_is(token, pair)
        sim = h.get("simulationResult") or {}
        row |= {"hp_success": h.get("simulationSuccess"),
                "hp_is_honeypot": (h.get("honeypotResult") or {}).get("isHoneypot"),
                "hp_buy_tax": sim.get("buyTax"), "hp_sell_tax": sim.get("sellTax"),
                "hp_reason": (h.get("honeypotResult") or {}).get("honeypotReason", "")}
        # Same threshold on their side: honeypot, or buy+sell tax over 50%.
        if row["hp_success"] is False and row["hp_is_honeypot"] is None:
            row["hp_flag"] = None
        else:
            taxes = (row["hp_buy_tax"] or 0) + (row["hp_sell_tax"] or 0)
            row["hp_flag"] = bool(row["hp_is_honeypot"]) or taxes > 50
    except Exception as e:
        row |= {"hp_reason": f"api error: {str(e)[:120]}", "hp_flag": None}
    return row


def summarise(rows: list[dict]) -> None:
    flag = {"True": True, "False": False, True: True, False: False}  # CSV rows hold strings
    both = [(flag[r["ours_flag"]], flag[r["hp_flag"]]) for r in rows
            if r.get("ours_flag") in flag and r.get("hp_flag") in flag]
    tp = sum(o and h for o, h in both)
    fp = sum(o and not h for o, h in both)
    fn = sum(h and not o for o, h in both)
    tn = sum(not o and not h for o, h in both)
    print(f"\ncompared {len(both)} of {len(rows)} pairs (rest: one side couldn't simulate)")
    print(f"both flag: {tp}   only ours: {fp}   only honeypot.is: {fn}   neither: {tn}")
    print(f"agreement: {(tp + tn) / max(len(both), 1):.1%}   -> {OUT}")


def main() -> None:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    pairs = load_pairs(want)
    rows = load_done()
    done = {r["token"].lower() for r in rows}
    todo = [p for p in pairs if checksum(p[0]).lower() not in done]
    print(f"{len(pairs) - len(todo)} already checked, {len(todo)} to go", file=sys.stderr)

    new_file = not OUT.exists()
    for i, (token, pair, depth) in enumerate(todo, 1):
        row = check_pair(token, pair, depth)
        with OUT.open("a", newline="") as f:  # one row at a time, so an interruption loses nothing
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
                new_file = False
            w.writerow(row)
        rows.append({k: str(v) for k, v in row.items()})
        mark = "AGREE" if row.get("ours_flag") == row.get("hp_flag") else "DIFF "
        print(f"[{i:>3}/{len(todo)}] {mark} ours={row.get('ours_flag')} hp={row.get('hp_flag')} "
              f"{row['token']}  {row.get('ours_error') or ''} | {row.get('hp_reason') or ''}", file=sys.stderr)
        time.sleep(1)  # be polite to the free API

    wanted = {checksum(t).lower() for t, _, _ in pairs}
    summarise([r for r in rows if r["token"].lower() in wanted])


if __name__ == "__main__":
    main()
