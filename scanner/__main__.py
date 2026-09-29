"""CLI: python -m scanner <token address> [--chain arc|base] [--json] [--explain]"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from web3 import Web3

from . import chain, config, indexer, report, simulate, static


def find_markets(token: str, log, sync_index: bool = True) -> list:
    """Every pool/pair to trade `token` through, best first: v4 pools (Arc), then V2 pairs.
    sync_index=False when something else (the API's background thread) keeps the index current."""
    pools = []
    if config.INDEX_START_BLOCK is not None:
        db = indexer.connect()
        indexed_to = indexer.synced_to(db)
        if indexed_to >= config.INDEX_START_BLOCK:
            if sync_index:
                log("catching the pool index up to the chain head...")
                indexer.sync(db, log=lambda _m: None)
                indexed_to = indexer.synced_to(db)
            pools = indexer.pools_for(token, db)
            # A server that just started from the snapshot may still be catching up: search only the
            # blocks the index hasn't reached, filtered by token (a few calls, not a full backfill).
            seen = {p.pool_id for p in pools}
            pools += [p for p in chain.find_v4_pools(token, indexed_to + 1) if p.pool_id not in seen]
        else:
            # No index at all yet: slow per-token log scan from the token's creation.
            log("finding creation block...")
            pools = chain.find_v4_pools(token, chain.find_creation_block(token),
                                        progress=lambda a, b, top: log(f"scanning pools, blocks {a:,}-{b:,} of {top:,}"))
        pools.sort(key=lambda p: p.liquidity, reverse=True)
    pairs = chain.find_v2_pairs(token)
    # Liquidity units differ between v4 and V2, so rank "has liquidity" first, then keep each list's order.
    return sorted(pools + pairs, key=lambda m: m.liquidity == 0)


def scan(token: str, log=lambda *_: None, sync_index: bool = True) -> report.ScanReport:
    if not chain.w3().eth.get_code(chain.checksum(token)):
        raise ValueError(f"{token} has no contract code on {config.NAME.title()}")
    # Independent reads run side by side; on a remote RPC, latency is most of a scan's time.
    with ThreadPoolExecutor(4) as ex:
        info = ex.submit(chain.token_info, token)
        st = ex.submit(static.analyse_token, token)
        markets = find_markets(token, log, sync_index)
        market = markets[0] if markets else None
        hook = ex.submit(static.analyse_hook, market.hooks) if isinstance(market, chain.Pool) else None
        trips = []
        if market:
            log(f"simulating {', '.join(f'{s:g}' for s in market.quote.sizes)} {market.quote.symbol} round trips...")
            trips = list(ex.map(lambda size: simulate.round_trip(market, token, size), market.quote.sizes))
        return report.assess(report.ScanReport(info.result(), st.result(), market, hook and hook.result(), trips,
                                               other_pools=markets[1:]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Arc Safety Scanner: free token risk check")
    ap.add_argument("token", help="token contract address")
    ap.add_argument("--chain", choices=sorted(config.CHAINS), default="arc")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--explain", action="store_true", help="add a plain-English explanation (uses LLM_PROVIDER)")
    args = ap.parse_args()
    if not Web3.is_address(args.token):
        ap.error("not a valid address")
    config.select(args.chain)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    r = scan(args.token, log=lambda msg: print(f"... {msg}", file=sys.stderr))
    explanation = None
    if args.explain:
        from . import explain
        explanation = explain.explain(r)
    if args.json:
        print(json.dumps({**r.to_dict(), "explanation": explanation}, indent=2, default=str))
    else:
        print(report.render_text(r))
        if explanation:
            print(f"\nIn plain English ({explanation['source']}):\n{explanation['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
