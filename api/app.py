"""Web API + page for the Arc Safety Scanner.

    uvicorn api.app:app --port 8000        # then open http://localhost:8000

Arc only: the scanner's chain config is process-wide, and Base exists for validation, not users.
A background thread keeps the v4 pool index at the chain head; request threads only read it. Until
the first backfill finishes (a few minutes on a fresh disk), scans fall back to the slower per-token
log scan, so the service works from the first second.
"""
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from web3 import Web3

from scanner import config, explain, indexer, report
from scanner.__main__ import scan
from scanner.chain import Pool
from scanner.simulate import gas_cost

CACHE_SECONDS = 300
MAX_CONCURRENT_SCANS = 3  # each scan makes dozens of RPC calls; protect the RPC's rate limit
PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"

app = FastAPI(title="Arc Safety Scanner", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["GET"], allow_headers=["*"])

_cache: dict[tuple[str, bool], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)
_index = {"synced_to": None, "error": None, "backfilled": False}


def _follow_chain_head() -> None:
    """Backfill once, then keep the pool index within a few blocks of the head, forever."""
    while True:
        try:
            db = indexer.connect()
            indexer.sync(db, log=lambda _m: None)
            _index.update(synced_to=indexer.synced_to(db), error=None, backfilled=True)
            db.close()
        except Exception as e:  # RPC hiccups: log and retry, never kill the thread
            _index["error"] = str(e)[:200]
        time.sleep(15)


@app.on_event("startup")
def _start_indexer() -> None:
    config.select("arc")
    threading.Thread(target=_follow_chain_head, daemon=True, name="pool-indexer").start()


def present(r: report.ScanReport) -> dict:
    """Display-ready fields, so the page never has to know about quote decimals or fee units."""
    trips = []
    for t in r.trips:
        row = {"size": report.amount(t.size, t.quote_symbol), "ok": t.buy_ok and t.sell_ok, "error": t.error}
        if row["ok"]:
            row |= {"back": report.amount(t.quote_received / t.quote_paid * t.size, t.quote_symbol),
                    "loss_percent": round(t.round_trip_loss * 100, 1),
                    "gas": report.gas_amount(gas_cost(t.buy_gas + t.sell_gas))}
        trips.append(row)
    market = None
    if r.pool is not None:
        market = {"kind": "Uniswap v4" if isinstance(r.pool, Pool) else "Uniswap V2",
                  "id": r.pool.pool_id if isinstance(r.pool, Pool) else r.pool.pair,
                  "fee_percent": round(report.fee_fraction(r.pool) * 100, 4),
                  "hook": r.pool.hooks if isinstance(r.pool, Pool) else None}
    others = [{"id": p.pool_id if isinstance(p, Pool) else p.pair,
               "fee_percent": round(report.fee_fraction(p) * 100, 4), "has_liquidity": p.liquidity > 0}
              for p in r.other_pools]
    s = r.static
    return {
        "chain": r.chain, "verdict": r.verdict, "reasons": r.reasons, "notes": r.notes,
        # Attacker-chosen strings: the page must render these as text, never as HTML.
        "token": {"address": r.token.address, "name": r.token.name, "symbol": r.token.symbol},
        "contract": {"launchpad_clone_of": s.implementation if s.is_minimal_proxy else None,
                     "upgradeable_implementation": s.implementation if s.upgradeable_proxy else None,
                     "owner": s.owner, "ownership_renounced": s.ownership_renounced,
                     "risky_functions": s.risky},
        "hook": {"permissions": r.hook.permissions, "can_block_swaps": r.hook.can_block_swaps,
                 "can_take_from_swaps": r.hook.can_take_from_swaps, "upgradeable": r.hook.upgradeable}
                if r.hook else None,
        "market": market, "other_markets": others, "round_trips": trips,
    }


@app.get("/health")
def health() -> dict:
    return {"ok": True, "chain_id": config.CHAIN_ID, "index": _index}


@app.get("/scan")
def scan_token(token: str = Query(..., description="token contract address on Arc"),
               explain_it: bool = Query(True, alias="explain")) -> dict:
    if not Web3.is_address(token):
        raise HTTPException(400, "Not a valid address.")
    key = (token.lower(), explain_it)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1] | {"cached": True}

    if not _scan_slots.acquire(timeout=60):
        raise HTTPException(503, "The scanner is busy. Try again in a minute.")
    try:
        r = scan(token, sync_index=False)
    except ValueError as e:  # e.g. no contract at that address
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Scan failed while reading Arc: {str(e)[:200]}")
    finally:
        _scan_slots.release()

    body = present(r)
    if explain_it:
        body["explanation"] = explain.explain(r)
    body["scanned_at"] = int(time.time())
    with _cache_lock:
        _cache[key] = (time.time(), body)
    return body | {"cached": False}


@app.get("/")
def page() -> FileResponse:
    return FileResponse(PAGE)
