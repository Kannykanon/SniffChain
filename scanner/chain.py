"""RPC access, token metadata, and Uniswap v4 pool discovery on Arc."""
from dataclasses import dataclass
from functools import lru_cache

from eth_abi import decode, encode
from requests.exceptions import ConnectionError, HTTPError, Timeout
from web3 import Web3
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from . import config

POOL_MANAGER_ABI = [
    {"name": "extsload", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "slot", "type": "bytes32"}], "outputs": [{"name": "", "type": "bytes32"}]},
]


@lru_cache(maxsize=1)
def w3() -> Web3:
    # The public RPC answers bursts with HTTP 429, so back off and retry (all methods here are reads).
    retry = ExceptionRetryConfiguration(errors=(ConnectionError, HTTPError, Timeout), retries=8, backoff_factor=0.5)
    return Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30},
                                  exception_retry_configuration=retry))


def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def call_raw(to: str, data: bytes, block="latest") -> bytes | None:
    """eth_call that returns None instead of raising when the call reverts."""
    try:
        return bytes(w3().eth.call({"to": checksum(to), "data": data}, block))
    except Exception:
        return None


def _selector(sig: str) -> bytes:
    return Web3.keccak(text=sig)[:4]


@dataclass
class TokenInfo:
    address: str
    name: str | None
    symbol: str | None
    decimals: int | None
    total_supply: int | None


def _decode_string(raw: bytes | None) -> str | None:
    if not raw:
        return None
    try:
        return decode(["string"], raw)[0]
    except Exception:
        # Some old tokens return bytes32 instead of string.
        return raw[:32].rstrip(b"\0").decode("utf-8", "replace") or None


def _decode_uint(raw: bytes | None) -> int | None:
    return decode(["uint256"], raw)[0] if raw and len(raw) >= 32 else None


def token_info(address: str) -> TokenInfo:
    # name/symbol are attacker-controlled strings: never interpret them, only display them.
    return TokenInfo(
        address=checksum(address),
        name=_decode_string(call_raw(address, _selector("name()"))),
        symbol=_decode_string(call_raw(address, _selector("symbol()"))),
        decimals=_decode_uint(call_raw(address, _selector("decimals()"))),
        total_supply=_decode_uint(call_raw(address, _selector("totalSupply()"))),
    )


def find_creation_block(address: str) -> int:
    """Binary search for the first block where `address` has code (needs archive state)."""
    addr = checksum(address)
    hi = w3().eth.block_number
    if not w3().eth.get_code(addr, hi):
        raise ValueError(f"{address} has no contract code on Arc")
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if w3().eth.get_code(addr, mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


@dataclass
class Pool:
    pool_id: str
    currency0: str
    currency1: str
    fee: int          # in pips (1_000_000 = 100%)
    tick_spacing: int
    hooks: str
    init_block: int
    liquidity: int = 0

    @property
    def key(self) -> tuple:
        return (self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)

    @property
    def quote(self) -> config.Quote:
        return quote_for(self.currency0) or quote_for(self.currency1)


@dataclass
class V2Pair:
    pair: str
    quote: config.Quote
    fee_bps: int
    quote_reserve: int  # how deep the pair is, in quote units

    @property
    def liquidity(self) -> int:
        return self.quote_reserve


def quote_for(address: str) -> config.Quote | None:
    return next((q for q in config.QUOTES if q.address == address.lower()), None)


def find_v2_pairs(token: str) -> list[V2Pair]:
    """V2 pairs of `token` against each quote asset, via factory.getPair (no log scanning needed)."""
    pairs = []
    for factory, fee_bps in config.V2_FACTORIES:
        for q in config.QUOTES:
            raw = call_raw(factory, _selector("getPair(address,address)") +
                           encode(["address", "address"], [checksum(token), checksum(q.address)]))
            pair = decode(["address"], raw)[0] if raw else None
            if not pair or int(pair, 16) == 0:
                continue
            reserve = _decode_uint(call_raw(q.address, _selector("balanceOf(address)") +
                                            encode(["address"], [pair]))) or 0
            pairs.append(V2Pair(checksum(pair), q, fee_bps, reserve))
    return sorted(pairs, key=lambda p: p.quote_reserve, reverse=True)


def _topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower()[2:]


def get_logs(params: dict, start: int, end: int) -> list:
    """eth_getLogs that halves the block range whenever the RPC says the request is too large."""
    try:
        return list(w3().eth.get_logs({**params, "fromBlock": start, "toBlock": end}))
    except Exception as e:
        if "too large" not in str(e) or start >= end:
            raise
    mid = (start + end) // 2
    return get_logs(params, start, mid) + get_logs(params, mid + 1, end)


def find_v4_pools(token: str, from_block: int, progress=None) -> list[Pool]:
    """All v4 pools pairing `token` with a quote asset, initialised at or after from_block."""
    latest = w3().eth.block_number
    quote_addrs = {q.address for q in config.QUOTES}
    pools: list[Pool] = []
    start = from_block
    while start <= latest:
        end = min(start + config.MAX_LOG_RANGE - 1, latest)
        if progress:
            progress(start, end, latest)
        # A token can be currency0 or currency1, and topics can't be OR-ed across positions.
        for topics in ([config.V4_INITIALIZE_TOPIC, None, _topic(token)],
                       [config.V4_INITIALIZE_TOPIC, None, None, _topic(token)]):
            params = {"address": checksum(config.UNISWAP_V4_POOL_MANAGER), "topics": topics}
            for log in get_logs(params, start, end):
                c0 = "0x" + log["topics"][2].hex()[-40:]
                c1 = "0x" + log["topics"][3].hex()[-40:]
                if not ({c0, c1} & quote_addrs):
                    continue
                fee, spacing, hooks, _price, _tick = decode(
                    ["uint24", "int24", "address", "uint160", "int24"], bytes(log["data"]))
                pools.append(Pool("0x" + log["topics"][1].hex().removeprefix("0x"),
                                  checksum(c0), checksum(c1), fee, spacing, checksum(hooks),
                                  log["blockNumber"]))
        start = end + 1
    for p in pools:
        p.liquidity = pool_liquidity(p.pool_id)
    return sorted(pools, key=lambda p: p.liquidity, reverse=True)


def pool_liquidity(pool_id: str) -> int:
    """Active liquidity via PoolManager.extsload (StateLibrary layout: pools[id] slot + 3)."""
    pm = w3().eth.contract(checksum(config.UNISWAP_V4_POOL_MANAGER), abi=POOL_MANAGER_ABI)
    state_slot = int.from_bytes(Web3.keccak(encode(["bytes32", "uint256"],
                                                   [bytes.fromhex(pool_id[2:]), config.V4_POOLS_SLOT])))
    raw = pm.functions.extsload((state_slot + 3).to_bytes(32, "big")).call()
    return int.from_bytes(raw[-16:], "big")
