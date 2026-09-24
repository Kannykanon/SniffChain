"""Deploys HoneypotSimulator to Arc mainnet. Needs DEPLOYER_PRIVATE_KEY (or ATTESTER_PRIVATE_KEY) in .env
and a little USDC for gas (~0.02 USDC at 2026-09-24 gas prices).

The scanner itself doesn't need this (it injects the code with a state override); deploying makes the
simulator a public, callable piece of Arc infrastructure and is the grant's "live on mainnet" link.

    python scripts/deploy.py            # dry run: prints the estimated cost only
    python scripts/deploy.py --send     # actually deploys
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import config  # noqa: E402
from scanner.chain import w3  # noqa: E402
from scanner.compile import load_artifact  # noqa: E402

key = (os.getenv("DEPLOYER_PRIVATE_KEY") or os.getenv("ATTESTER_PRIVATE_KEY") or "").strip()
if not key:
    sys.exit("Set DEPLOYER_PRIVATE_KEY (or ATTESTER_PRIVATE_KEY) in .env")

web3 = w3()
assert web3.eth.chain_id == config.CHAIN_ID, "not connected to Arc mainnet"
acct = web3.eth.account.from_key(key)
art = load_artifact()
tx = web3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]).constructor().build_transaction({
    "from": acct.address, "nonce": web3.eth.get_transaction_count(acct.address), "chainId": config.CHAIN_ID,
})
cost = tx["gas"] * tx.get("maxFeePerGas", web3.eth.gas_price) / 10 ** config.NATIVE_DECIMALS
print(f"Deployer {acct.address}, balance {web3.eth.get_balance(acct.address) / 1e18:.4f} USDC, "
      f"max cost ~{cost:.4f} USDC")

if "--send" not in sys.argv:
    print("Dry run. Re-run with --send to deploy.")
    sys.exit(0)
if web3.eth.get_balance(acct.address) < cost * 10 ** config.NATIVE_DECIMALS:
    sys.exit(f"Not enough USDC: fund {acct.address} on Arc with at least {cost:.4f} USDC first.")

tx_hash = web3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
print(f"Deployed at {receipt.contractAddress} (tx {tx_hash.hex()})")
print("Add DEPLOYED_SIMULATOR=<address> to .env and README.")
