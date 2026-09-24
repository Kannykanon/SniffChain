"""End-to-end simulator test on live Arc mainnet state, with nothing deployed or broadcast.

HoneypotScenario creates a token, opens a Uniswap v4 pool against USDC, adds liquidity and runs
HoneypotSimulator against it, all inside a single eth_call.

    python tests/test_simulator.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import config  # noqa: E402
from scanner.chain import checksum, w3  # noqa: E402
from scanner.compile import load_artifact  # noqa: E402
from scanner.simulate import parse_legs  # noqa: E402

SCENARIO = checksum("0x5ce0a10000000000000000000000000000000001")
# Start price $0.001 per token (18 decimals) against USDC (6 decimals), for either currency order.
SQRT_PRICE_TOKEN_IS_0 = 2505414483750479311864
SQRT_PRICE_TOKEN_IS_1 = 2505414483750479311864138015696063230
LIQUIDITY = 316_227_766_016_837_933  # full range, ~$10k of USDC on each side
BUY_USDC = 100 * 10**6               # $100 buy -> ~100k tokens at the start price


def run_case(max_sell: int):
    scenario, sim = load_artifact("HoneypotScenario", "test/HoneypotScenario.sol"), load_artifact()
    overrides = {
        SCENARIO: {"code": scenario["runtime"], "balance": 100_000 * 10**18},     # funds the liquidity
        checksum(config.SIMULATOR_ADDRESS): {"code": sim["runtime"], "balance": 1_000 * 10**18},
    }
    c = w3().eth.contract(SCENARIO, abi=scenario["abi"])
    buy, sell = c.functions.run(
        checksum(config.UNISWAP_V4_POOL_MANAGER), checksum(config.SIMULATOR_ADDRESS), max_sell,
        SQRT_PRICE_TOKEN_IS_0, SQRT_PRICE_TOKEN_IS_1, LIQUIDITY, BUY_USDC,
    ).call({"from": checksum(config.SIMULATION_CALLER), "gas": 30_000_000}, "latest", overrides)
    return parse_legs(100, "USDC", buy, sell)


def main() -> int:
    failures = 0

    def check(name, cond, detail):
        nonlocal failures
        print(f"[{'pass' if cond else 'FAIL'}] {name}: {detail}")
        failures += not cond

    normal = run_case(max_sell=2**256 - 1)
    check("normal token sells", normal.buy_ok and normal.sell_ok,
          f"loss {normal.round_trip_loss:.2%}, error={normal.error}")
    # 0.3% fee each way plus a little price impact on ~$10k of liquidity.
    check("normal token loss is just fees + impact", 0.005 < normal.round_trip_loss < 0.03,
          f"{normal.round_trip_loss:.2%}")

    blocked = run_case(max_sell=0)
    check("honeypot: buy works", blocked.buy_ok, f"received {blocked.tokens_received / 1e18:,.0f} tokens")
    check("honeypot: sell detected as failing", not blocked.sell_ok, str(blocked.error))
    check("honeypot: revert reason surfaced", "sell blocked" in (blocked.error or ""), str(blocked.error))

    # Allow sells up to 50k tokens; the $100 buy gets ~100k, so the full sell must fail.
    capped = run_case(max_sell=50_000 * 10**18)
    check("size-capped honeypot: large sell fails", capped.buy_ok and not capped.sell_ok, str(capped.error))

    print("\nall passed" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
