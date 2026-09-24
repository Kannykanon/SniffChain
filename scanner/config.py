"""Per-chain constants. Arc is the default; select("base") switches every module to Base.

Every address here was checked on-chain (Arc 2026-09-23, Base 2026-09-24).
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Quote:
    """An asset we buy with. `funding` is how the simulator gets a balance inside eth_call:
    "native" = native-balance override (Arc USDC); "slot" = override the ERC-20 balance storage slot."""
    address: str
    symbol: str
    decimals: int
    sizes: tuple[float, ...]   # buy sizes in whole units; several sizes catch size-dependent sells
    funding: str


NATIVE = "0x0000000000000000000000000000000000000000"
NATIVE_DECIMALS = 18
# Arbitrary, unused addresses the simulator runs at / is called from inside eth_call.
SIMULATOR_ADDRESS = "0x5afe5afe5afe5afe5afe5afe5afe5afe5afe5afe"
SIMULATION_CALLER = "0x000000000000000000000000000000000000beef"
# Initialize(bytes32 indexed id, address indexed currency0, address indexed currency1,
#            uint24 fee, int24 tickSpacing, address hooks, uint160 sqrtPriceX96, int24 tick)
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
V4_POOLS_SLOT = 6

CHAINS = {
    "arc": dict(
        CHAIN_ID=5042,
        NATIVE_SYMBOL="USDC",
        RPC_URL=os.getenv("ARC_RPC_URL", "https://rpc.mainnet.arc.io"),
        # USDC's ERC-20 predeploy: 6 decimals as ERC-20; the same balance is 18 decimals natively.
        USDC="0x3600000000000000000000000000000000000000",
        QUOTES=(Quote("0x3600000000000000000000000000000000000000", "USDC", 6, (10, 100, 1000), "native"),
                # v4 pools can also price in the native currency (address 0), which on Arc is USDC too.
                Quote(NATIVE, "USDC", 18, (10, 100, 1000), "native")),
        UNISWAP_V4_POOL_MANAGER="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        UNISWAP_V4_DEPLOY_BLOCK=1_948_056,
        # Our HoneypotSimulator, deployed 2026-09-24 (block 22,507,996). The scanner itself injects the
        # same bytecode via a code override; this public copy is for anyone else to call.
        SIMULATOR_DEPLOYED="0x59dFDB2c3c15529CBD0E999Cca42357C103E5E10",
        # The pool index starts 2026-09-15 00:00 UTC, a day before public launch. The ~19M blocks
        # before it held only a handful of test pools and cost most of the backfill time.
        INDEX_START_BLOCK=20_900_406,
        # (factory, fee in basis points). Arc's V2 forks aren't added yet.
        V2_FACTORIES=(),
        MAX_LOG_RANGE=100_000,
    ),
    "base": dict(
        CHAIN_ID=8453,
        NATIVE_SYMBOL="ETH",
        RPC_URL=os.getenv("BASE_RPC_URL", "https://mainnet.base.org"),
        USDC="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        QUOTES=(
            Quote("0x4200000000000000000000000000000000000006", "WETH", 18, (0.003, 0.03, 0.3), "slot"),
            Quote("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC", 6, (10, 100, 1000), "slot"),
        ),
        UNISWAP_V4_POOL_MANAGER="0x498581ff718922c3f8e6a244956af099b2652b2b",
        UNISWAP_V4_DEPLOY_BLOCK=0,
        INDEX_START_BLOCK=None,  # no v4 pool index on Base; V2 pairs are found with getPair()
        V2_FACTORIES=(("0x8909dc15e40173ff4699343b6eb8132c65e18ec6", 30),),  # Uniswap V2
        MAX_LOG_RANGE=10_000,
    ),
}

NAME = "arc"
globals().update(CHAINS[NAME])


def select(name: str) -> None:
    """Point every module at another chain. Call before any RPC use."""
    global NAME
    NAME = name
    globals().update(CHAINS[name])
    from .chain import w3
    w3.cache_clear()
