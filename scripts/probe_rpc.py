"""Checks the RPC features the scanner depends on. Run: python scripts/probe_rpc.py
(set ARC_RPC_URL in .env to probe a different endpoint)

Results on https://rpc.mainnet.arc.io, 2026-09-23:
  chain id 5042, archive state yes, eth_call state overrides yes (code + balance),
  native-balance override visible through USDC ERC-20 balanceOf: yes,
  debug_traceCall: NOT supported, eth_getLogs: max 100k blocks and a result-size cap.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3  # noqa: E402

from scanner import config  # noqa: E402
from scanner.chain import w3 as client  # noqa: E402

w3 = client()
print(f"Probing {config.RPC_URL}")
probe = Web3.to_checksum_address("0x000000000000000000000000000000000000beef")
usdc = Web3.to_checksum_address(config.USDC)
balance_of = "0x70a08231" + "0" * 24 + probe[2:].lower()


def check(name, fn):
    try:
        print(f"[ok]   {name}: {fn()}")
    except Exception as e:
        print(f"[FAIL] {name}: {str(e)[:120]}")


check("chain id", lambda: w3.eth.chain_id)
check("client", lambda: w3.client_version)
check("archive state (USDC code at block 1,000,000)", lambda: len(w3.eth.get_code(usdc, 1_000_000)) > 0)
check("USDC decimals", lambda: int(w3.eth.call({"to": usdc, "data": "0x313ce567"}).hex(), 16))
check("code override", lambda: w3.eth.call(
    {"to": probe, "data": "0x"}, "latest", {probe: {"code": "0x602a60005260206000f3"}}).hex())
check("native balance override seen by USDC.balanceOf (expect 1000 USDC)", lambda: int(w3.eth.call(
    {"to": usdc, "data": balance_of}, "latest", {probe: {"balance": 1000 * 10**18}}).hex(), 16) / 1e6)
check("debug_traceCall", lambda: w3.provider.make_request(
    "debug_traceCall", [{"to": usdc, "data": "0x313ce567"}, "latest", {"tracer": "callTracer"}]).get("error") or "supported")
check("eth_getLogs over 100,001 blocks", lambda: len(w3.eth.get_logs(
    {"address": usdc, "fromBlock": 0, "toBlock": 100_000})))
