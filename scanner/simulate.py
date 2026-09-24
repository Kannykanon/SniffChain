"""Runs HoneypotSimulator inside eth_call with state overrides; nothing is broadcast or paid."""
from dataclasses import dataclass
from functools import lru_cache

from eth_abi import decode, encode
from web3 import Web3

from . import config
from .chain import Pool, V2Pair, checksum, w3
from .compile import load_artifact

# Revert selectors worth naming in the report.
KNOWN_ERRORS = {
    "5212cba1": "CurrencyNotSettled (token taxes or blocks transfers into the pool)",
    "4e487b71": "Panic(uint256)",
    "90bfb865": "WrappedError (a hook or token reverted inside the swap)",
    "a9e35b2f": "HookCallFailed",
    "486aa307": "PoolNotInitialized",
}


@dataclass
class RoundTrip:
    size: float           # buy size in whole quote units
    quote_symbol: str
    buy_ok: bool
    sell_ok: bool
    quote_paid: int
    tokens_quoted: int
    tokens_received: int
    quote_quoted: int
    quote_received: int
    buy_gas: int
    sell_gas: int
    error: str | None

    @property
    def buy_transfer_tax(self) -> float:
        return 1 - self.tokens_received / self.tokens_quoted if self.tokens_quoted else 0.0

    @property
    def round_trip_loss(self) -> float:
        """Share of the quote asset lost buying and immediately selling: fees + taxes (+ price impact)."""
        return 1 - self.quote_received / self.quote_paid if self.quote_paid else 0.0


def describe_revert(err: bytes) -> str:
    if not err:
        return "reverted without a reason"
    sel = err[:4].hex()
    if sel == "08c379a0":
        try:
            return "revert: " + decode(["string"], err[4:])[0]
        except Exception:
            pass
    return KNOWN_ERRORS.get(sel, f"custom error 0x{sel}")


def parse_legs(size: float, symbol: str, buy: tuple, sell: tuple) -> RoundTrip:
    # Leg tuple: (ok, err, paid, quoted, received, gasUsed)
    # A buy that "succeeds" with zero output means no usable liquidity; the contract then skips the
    # sell, so its empty leg must not be read as a failed sell.
    buy_ok = buy[0] and buy[4] > 0
    error = None
    if not buy[0]:
        error = "buy failed: " + describe_revert(bytes(buy[1]))
    elif not buy_ok:
        error = "buy returned no tokens (pool has no usable liquidity)"
    elif not sell[0]:
        error = "sell failed: " + describe_revert(bytes(sell[1]))
    return RoundTrip(
        size=size, quote_symbol=symbol, buy_ok=buy_ok, sell_ok=sell[0],
        quote_paid=buy[2], tokens_quoted=buy[3], tokens_received=buy[4],
        quote_quoted=sell[3], quote_received=sell[4],
        buy_gas=buy[5], sell_gas=sell[5], error=error,
    )


@lru_cache(maxsize=None)
def balance_slot(chain: str, token: str) -> tuple[int, str]:
    """Find the storage slot of `token`'s balances mapping by overriding candidates and reading
    balanceOf back. Returns (slot, layout) where layout is "solidity" or "vyper"."""
    holder = config.SIMULATOR_ADDRESS
    magic = 0x1234567890ABCDEF
    call = {"to": checksum(token), "data": "0x70a08231" + encode(["address"], [holder]).hex()}
    for slot in range(0, 60):
        for layout in ("solidity", "vyper"):
            key = Web3.keccak(encode(["address", "uint256"], [holder, slot]) if layout == "solidity"
                              else encode(["uint256", "address"], [slot, holder]))
            try:
                out = w3().eth.call(call, "latest", {checksum(token): {"stateDiff": {key.hex(): "0x" + f"{magic:064x}"}}})
            except Exception:
                continue
            if int.from_bytes(out, "big") == magic:
                return slot, layout
    raise RuntimeError(f"couldn't find the balance slot of {token}")


def _funding_overrides(quote: config.Quote, amount: int, sim_code: str) -> dict:
    sim = checksum(config.SIMULATOR_ADDRESS)
    if quote.funding == "native":
        # Arc: USDC's ERC-20 reads the native balance, so one native override funds both paths.
        native = amount * 10 ** (config.NATIVE_DECIMALS - quote.decimals) + 10**18
        return {sim: {"code": sim_code, "balance": native}}
    slot, layout = balance_slot(config.NAME, quote.address)
    key = Web3.keccak(encode(["address", "uint256"], [sim, slot]) if layout == "solidity"
                      else encode(["uint256", "address"], [slot, sim]))
    return {sim: {"code": sim_code},
            checksum(quote.address): {"stateDiff": {key.hex(): "0x" + f"{amount:064x}"}}}


def round_trip(market: Pool | V2Pair, token: str, size: float) -> RoundTrip:
    art = load_artifact()
    sim = w3().eth.contract(checksum(config.SIMULATOR_ADDRESS), abi=art["abi"])
    quote = market.quote
    amount_in = int(size * 10 ** quote.decimals)
    overrides = _funding_overrides(quote, amount_in, art["runtime"])
    tx = {"from": checksum(config.SIMULATION_CALLER), "gas": 30_000_000}

    if isinstance(market, V2Pair):
        fn = sim.functions.simulateV2(checksum(market.pair), checksum(token), checksum(quote.address),
                                      amount_in, market.fee_bps)
    else:
        fn = sim.functions.simulate(checksum(config.UNISWAP_V4_POOL_MANAGER), market.key,
                                    checksum(token), amount_in)
    buy, sell = fn.call(tx, "latest", overrides)
    return parse_legs(size, quote.symbol, buy, sell)


def gas_cost(gas: int) -> float:
    """Gas cost in the chain's native unit (on Arc that's USDC, i.e. dollars)."""
    return gas * w3().eth.gas_price / 10 ** config.NATIVE_DECIMALS
