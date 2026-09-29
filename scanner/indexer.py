"""Local SQLite index of every Uniswap v4 pool on Arc.

Per-token log scans are slow on the public RPC (range caps, 429s, a chain that started in May 2026),
so we follow PoolManager Initialize events once and answer "which pools hold token X" from disk.

    python -m scanner.indexer              # backfill / catch up to the chain head
    python -m scanner.indexer --snapshot   # then write the snapshot the server starts from

Hosts with a throwaway disk (Render's free plan) lose the index on every restart, so the repo ships a
snapshot of USDC-paired pools: the server restores it in seconds and only syncs the blocks after it.
"""
import gzip
import sqlite3
import sys
import time
from pathlib import Path

from eth_abi import decode

from . import config
from .chain import Pool, checksum, read_liquidity, w3

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pools.sqlite"  # Arc only
SNAPSHOT_PATH = DB_PATH.with_name("pools_snapshot.tsv.gz")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    pool_id TEXT PRIMARY KEY, currency0 TEXT, currency1 TEXT,
    fee INTEGER, tick_spacing INTEGER, hooks TEXT, init_block INTEGER);
CREATE INDEX IF NOT EXISTS pools_c0 ON pools(currency0);
CREATE INDEX IF NOT EXISTS pools_c1 ON pools(currency1);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER);
"""


_schema_ready = False


def connect() -> sqlite3.Connection:
    global _schema_ready
    DB_PATH.parent.mkdir(exist_ok=True)
    # The API reads from request threads while a background thread writes: wait on locks, don't fail.
    db = sqlite3.connect(DB_PATH, timeout=30)
    if not _schema_ready:
        # WAL lets readers (scans, /health) see the last commit while a long write (snapshot restore)
        # is still running; the schema script needs a write lock, so run it once per process.
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        _schema_ready = True
    return db


def synced_to(db) -> int:
    row = db.execute("SELECT v FROM meta WHERE k='synced_to'").fetchone()
    return row[0] if row else -1


def sync(db, until: int | None = None, log=print, yield_to=lambda: False) -> None:
    """Pull Initialize logs from the last synced block to `until` (default: head), resizing chunks
    adaptively: halve when the RPC says the request is too large, grow after successes.
    While yield_to() is true (e.g. a user's scan is running) it waits between chunks, so a long
    backfill never competes with scans for the RPC's rate limit."""
    head = until or w3().eth.block_number
    start, chunk = max(synced_to(db) + 1, config.INDEX_START_BLOCK), config.MAX_LOG_RANGE
    streak = 0  # consecutive successes; growing after every success just re-hits the RPC's limit
    params = {"address": checksum(config.UNISWAP_V4_POOL_MANAGER), "topics": [config.V4_INITIALIZE_TOPIC]}
    t0, done0 = time.time(), start
    while start <= head:
        while yield_to():
            time.sleep(0.5)
        end = min(start + chunk - 1, head)
        try:
            logs = w3().eth.get_logs({**params, "fromBlock": start, "toBlock": end})
        except Exception as e:
            if "too large" in str(e) and chunk > 1:
                chunk, streak = max(1, chunk // 2), 0
                continue
            raise
        rows = []
        for lg in logs:
            fee, spacing, hooks, _price, _tick = decode(["uint24", "int24", "address", "uint160", "int24"],
                                                        bytes(lg["data"]))
            rows.append(("0x" + lg["topics"][1].hex().removeprefix("0x"),
                         "0x" + lg["topics"][2].hex()[-40:], "0x" + lg["topics"][3].hex()[-40:],
                         fee, spacing, hooks.lower(), lg["blockNumber"]))
        db.executemany("INSERT OR IGNORE INTO pools VALUES (?,?,?,?,?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('synced_to', ?)", (end,))
        db.commit()
        rate = (end - done0 + 1) / max(time.time() - t0, 1e-9)
        log(f"block {end:,}/{head:,}  +{len(rows)} pools  chunk {chunk:,}  ~{(head - end) / rate / 60:.0f} min left")
        start = end + 1
        streak += 1
        if streak >= 10:
            chunk, streak = min(chunk * 2, config.MAX_LOG_RANGE), 0


def restore_snapshot(db) -> bool:
    """Load the shipped snapshot if it's ahead of the local index. Returns whether it loaded."""
    if not SNAPSHOT_PATH.exists():
        return False
    with gzip.open(SNAPSHOT_PATH, "rt") as f:
        snap_to = int(f.readline().split()[-1])  # header: "# synced_to <block>"
        if snap_to <= synced_to(db):
            return False
        rows = (line.rstrip("\n").split("\t") for line in f)
        db.executemany("INSERT OR IGNORE INTO pools VALUES (?,?,?,?,?,?,?)", rows)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('synced_to', ?)", (snap_to,))
    db.commit()
    return True


def export_snapshot(db) -> int:
    """Write the USDC-paired pools (the only ones scans look up) to SNAPSHOT_PATH."""
    quotes = tuple(q.address for q in config.QUOTES)
    marks = ",".join("?" * len(quotes))
    rows = db.execute(f"SELECT * FROM pools WHERE currency0 IN ({marks}) OR currency1 IN ({marks}) "
                      "ORDER BY init_block", (*quotes, *quotes)).fetchall()
    with gzip.open(SNAPSHOT_PATH, "wt", compresslevel=9, newline="\n") as f:
        f.write(f"# synced_to {synced_to(db)}\n")
        f.writelines("\t".join(map(str, r)) + "\n" for r in rows)
    return len(rows)


def pools_for(token: str, db=None) -> list[Pool]:
    """USDC-paired pools for `token`, most liquid first. Liquidity is read live, not from the index."""
    db = db or connect()
    t = token.lower()
    quotes = tuple(q.address for q in config.QUOTES)
    marks = ",".join("?" * len(quotes))
    rows = db.execute(
        f"SELECT * FROM pools WHERE (currency0=? AND currency1 IN ({marks})) OR (currency1=? AND currency0 IN ({marks}))",
        (t, *quotes, t, *quotes)).fetchall()
    pools = [Pool(pid, checksum(c0), checksum(c1), fee, sp, checksum(h), blk) for pid, c0, c1, fee, sp, h, blk in rows]
    read_liquidity(pools)
    return sorted(pools, key=lambda p: p.liquidity, reverse=True)


if __name__ == "__main__":
    db = connect()
    if "--snapshot" in sys.argv:
        print(f"wrote {export_snapshot(db):,} pools (to block {synced_to(db):,}) to {SNAPSHOT_PATH}", file=sys.stderr)
    else:
        sync(db, log=lambda m: print(m, file=sys.stderr))
