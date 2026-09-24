"""Bytecode-level checks: proxies, ownership, risky functions, v4 hook permissions, clone fingerprint."""
from dataclasses import dataclass, field

from eth_abi import decode
from web3 import Web3

from .chain import call_raw, checksum, w3

EIP1967_IMPL_SLOT = int("360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", 16)
DEAD_OWNERS = {"0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}

# Functions whose presence lets a privileged account change who can trade or how much is taken.
RISKY_FUNCTIONS = {
    "mint(address,uint256)": "owner can mint new supply",
    "mint(uint256)": "owner can mint new supply",
    "blacklist(address)": "can blacklist wallets",
    "addToBlacklist(address)": "can blacklist wallets",
    "setBlacklist(address,bool)": "can blacklist wallets",
    "blacklistAddress(address,bool)": "can blacklist wallets",
    "setBots(address[],bool)": "can mark wallets as bots (usually blocks selling)",
    "pause()": "trading can be paused",
    "setMaxTxAmount(uint256)": "max transaction size can be changed (can block sells)",
    "setMaxWalletSize(uint256)": "max wallet size can be changed",
    "setFees(uint256,uint256)": "taxes can be changed after launch",
    "setTaxes(uint256,uint256)": "taxes can be changed after launch",
    "setSellFee(uint256)": "sell tax can be changed after launch",
    "updateFees(uint256,uint256)": "taxes can be changed after launch",
    "setTradingEnabled(bool)": "trading can be switched off",
    "enableTrading(bool)": "trading can be switched off",
    "excludeFromFees(address,bool)": "owner can exempt chosen wallets from taxes",
    "upgradeTo(address)": "contract logic can be replaced",
    "upgradeToAndCall(address,bytes)": "contract logic can be replaced",
}

# Uniswap v4 encodes hook permissions in the low 14 bits of the hook address.
HOOK_FLAGS = {
    13: "beforeInitialize", 12: "afterInitialize",
    11: "beforeAddLiquidity", 10: "afterAddLiquidity",
    9: "beforeRemoveLiquidity", 8: "afterRemoveLiquidity",
    7: "beforeSwap", 6: "afterSwap",
    5: "beforeDonate", 4: "afterDonate",
    3: "beforeSwapReturnsDelta", 2: "afterSwapReturnsDelta",
    1: "afterAddLiquidityReturnsDelta", 0: "afterRemoveLiquidityReturnsDelta",
}


@dataclass
class StaticReport:
    is_minimal_proxy: bool = False
    upgradeable_proxy: bool = False
    implementation: str | None = None
    owner: str | None = None
    ownership_renounced: bool | None = None
    risky: dict[str, str] = field(default_factory=dict)   # signature -> why it matters
    fingerprint: str = ""                                  # clone-cluster key


def push4_selectors(code: bytes) -> set[bytes]:
    """Every PUSH4 immediate, walking opcodes properly so data bytes aren't misread as opcodes."""
    out, i = set(), 0
    while i < len(code):
        op = code[i]
        if op == 0x63:
            out.add(code[i + 1:i + 5])
        if 0x60 <= op <= 0x7F:
            i += op - 0x5F
        i += 1
    return out


def strip_metadata(code: bytes) -> bytes:
    """Drop the trailing CBOR metadata so recompiles of the same template share a fingerprint."""
    if len(code) < 2:
        return code
    meta_len = int.from_bytes(code[-2:], "big")
    return code[:-(meta_len + 2)] if meta_len + 2 <= len(code) else code


def minimal_proxy_target(code: bytes) -> str | None:
    """EIP-1167 clone: 363d3d373d3d3d363d73 <20-byte impl> 5af43d82803e903d91602b57fd5bf3"""
    if len(code) == 45 and code[:10].hex() == "363d3d373d3d3d363d73":
        return checksum("0x" + code[10:30].hex())
    return None


def analyse_token(address: str) -> StaticReport:
    r = StaticReport()
    addr = checksum(address)
    code = bytes(w3().eth.get_code(addr))

    impl = minimal_proxy_target(code)
    if impl:
        r.is_minimal_proxy, r.implementation = True, impl
    else:
        slot = int.from_bytes(w3().eth.get_storage_at(addr, EIP1967_IMPL_SLOT), "big")
        if slot:
            r.upgradeable_proxy, r.implementation = True, checksum("0x" + f"{slot:040x}"[-40:])
    logic = bytes(w3().eth.get_code(r.implementation)) if r.implementation else code

    raw_owner = call_raw(addr, Web3.keccak(text="owner()")[:4])
    if raw_owner and len(raw_owner) >= 32:
        r.owner = checksum(decode(["address"], raw_owner)[0])
        r.ownership_renounced = r.owner.lower() in DEAD_OWNERS

    selectors = push4_selectors(logic)
    for sig, why in RISKY_FUNCTIONS.items():
        if Web3.keccak(text=sig)[:4] in selectors:
            r.risky[sig] = why

    # Clones of one launchpad template share an implementation; everything else is keyed by code hash.
    r.fingerprint = ("impl:" + r.implementation.lower()) if r.is_minimal_proxy \
        else "code:" + Web3.keccak(strip_metadata(logic)).hex()
    return r


@dataclass
class HookReport:
    address: str
    permissions: list[str]
    can_block_swaps: bool
    can_take_from_swaps: bool
    upgradeable: bool
    owner: str | None


def analyse_hook(hook: str) -> HookReport | None:
    if int(hook, 16) == 0:
        return None
    bits = int(hook, 16) & 0x3FFF
    perms = [name for bit, name in HOOK_FLAGS.items() if bits >> bit & 1]
    slot = int.from_bytes(w3().eth.get_storage_at(checksum(hook), EIP1967_IMPL_SLOT), "big")
    raw_owner = call_raw(hook, Web3.keccak(text="owner()")[:4])
    owner = checksum(decode(["address"], raw_owner)[0]) if raw_owner and len(raw_owner) >= 32 else None
    return HookReport(
        address=checksum(hook),
        permissions=perms,
        can_block_swaps="beforeSwap" in perms or "afterSwap" in perms,
        can_take_from_swaps="beforeSwapReturnsDelta" in perms or "afterSwapReturnsDelta" in perms,
        upgradeable=bool(slot),
        owner=owner,
    )
