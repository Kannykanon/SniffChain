"""CLI: python -m scanner <token address> [--chain arc|base] [--json] [--explain]"""
import argparse
import json
import sys

from web3 import Web3

from . import chain, config, indexer, report, simulate, static


def find_markets(token: str, log) -> list:
    """Every pool/pair to trade `token` through, best first: v4 pools (Arc), then V2 pairs."""
    pools = []
    if config.INDEX_START_BLOCK is not None:
        db = indexer.connect()
        if indexer.synced_to(db) >= config.INDEX_START_BLOCK:
            log("catching the pool index up to the chain head...")
            indexer.sync(db, log=lambda _m: None)
            pools = indexer.pools_for(token, db)
        if not pools:
            # No index yet, or the token predates it: slow per-token log scan.
            log("finding creation block...")
            created = chain.find_creation_block(token)
            pools = chain.find_v4_pools(
                token, created, progress=lambda a, b, top: log(f"scanning pools, blocks {a:,}-{b:,} of {top:,}"))
    pairs = chain.find_v2_pairs(token)
    # Liquidity units differ between v4 and V2, so rank "has liquidity" first, then keep each list's order.
    return sorted(pools + pairs, key=lambda m: m.liquidity == 0)


def scan(token: str, log=lambda *_: None) -> report.ScanReport:
    info = chain.token_info(token)
    log("static checks...")
    st = static.analyse_token(token)
    markets = find_markets(token, log)
    market = markets[0] if markets else None
    trips = []
    if market:
        for size in market.quote.sizes:
            log(f"simulating {size:g} {market.quote.symbol} round trip...")
            trips.append(simulate.round_trip(market, token, size))
    hook = static.analyse_hook(market.hooks) if isinstance(market, chain.Pool) else None
    return report.assess(report.ScanReport(info, st, market, hook, trips, other_pools=markets[1:]))


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
