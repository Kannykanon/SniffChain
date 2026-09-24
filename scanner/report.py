"""Deterministic risk verdict. The AI layer (explain.py) may only explain this, never override it."""
from dataclasses import asdict, dataclass, field

from . import config
from .chain import Pool, TokenInfo, V2Pair
from .simulate import RoundTrip, gas_cost
from .static import HookReport, StaticReport

HIGH, MEDIUM, LOW, UNKNOWN = "HIGH", "MEDIUM", "LOW", "UNKNOWN"

# v4 lets a pool charge up to 100% in LP fees. Anything above this is a trap, not a market.
TRAP_FEE_PIPS = 100_000  # 10%


@dataclass
class ScanReport:
    token: TokenInfo
    static: StaticReport
    pool: Pool | V2Pair | None
    hook: HookReport | None
    trips: list[RoundTrip]
    other_pools: list[Pool | V2Pair] = field(default_factory=list)
    chain: str = field(default_factory=lambda: config.NAME)
    verdict: str = UNKNOWN
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # worth knowing, doesn't change the verdict

    def to_dict(self) -> dict:
        d = asdict(self)
        for t, trip in zip(d["trips"], self.trips):
            t["round_trip_loss"] = trip.round_trip_loss
            t["gas_cost_native"] = gas_cost(trip.buy_gas + trip.sell_gas)
        return d


def fee_fraction(market: Pool | V2Pair) -> float:
    return market.fee / 1_000_000 if isinstance(market, Pool) else market.fee_bps / 10_000


def amount(x: float, symbol: str) -> str:
    return f"${x:,.2f}" if symbol == "USDC" else f"{x:,.6g} {symbol}"


def gas_amount(x: float) -> str:
    """Gas is fractions of a cent, so it gets more precision than trade amounts."""
    return f"${x:.4f}" if config.NATIVE_SYMBOL == "USDC" else f"{x:.3g} {config.NATIVE_SYMBOL}"


def assess(r: ScanReport) -> ScanReport:
    high, medium = [], []

    if r.pool is None:
        r.verdict = UNKNOWN
        r.reasons = ["No pool or pair against a supported quote asset found, so selling could not be simulated."]
        return r

    fee = fee_fraction(r.pool)
    if fee > TRAP_FEE_PIPS / 1_000_000:
        high.append(f"The main pool charges a {fee:.0%} LP fee on every trade.")
    traps = [p for p in r.other_pools if isinstance(p, Pool) and p.fee > TRAP_FEE_PIPS]
    if traps:
        # Spell out that these are *other* pools: the simulated trades above didn't go through them.
        fees = ", ".join(f"{p.fee / 10_000:g}%" for p in traps)
        r.notes.append(f"The simulated trades used a pool with a {fee:.2%} fee. Separately, {len(traps)} other "
                       f"pool(s) for this token charge extreme LP fees ({fees}); trading through one of "
                       "those would cost far more.")
    done = [t for t in r.trips if t.buy_ok]
    for t in r.trips:
        if t.buy_ok and not t.sell_ok:
            high.append(f"Buying {amount(t.size, t.quote_symbol)} works but selling it back fails ({t.error}).")
    if r.trips and not done:
        medium.append(f"Could not even buy through the pool ({r.trips[0].error}).")

    sold = [t for t in done if t.sell_ok]
    if sold:
        worst = max(sold, key=lambda t: t.round_trip_loss)
        extra = worst.round_trip_loss - 2 * fee
        if worst.round_trip_loss > 0.5:
            high.append(f"A {amount(worst.size, worst.quote_symbol)} buy-then-sell loses {worst.round_trip_loss:.0%}.")
        elif extra > 0.1:
            # Taxes this size are usually disclosed rather than hidden, but a buyer still loses a lot.
            medium.append(f"Roughly {extra:.0%} of a round trip goes to token taxes, on top of the {fee:.2%} pool fee each way.")
        elif extra > 0.01:
            r.notes.append(f"About {extra:.0%} of a round trip goes to token taxes, on top of the {fee:.2%} pool fee each way.")
        small, large = min(sold, key=lambda t: t.size), max(sold, key=lambda t: t.size)
        if large.round_trip_loss - small.round_trip_loss > 0.1:
            medium.append("Losses grow sharply with trade size, which can be a hidden large-sell penalty or very thin liquidity.")

    if isinstance(r.pool, V2Pair) and r.trips:
        depth = r.pool.quote_reserve / 10 ** r.pool.quote.decimals
        biggest = max(t.size for t in r.trips)
        if depth < biggest:
            r.notes.append(f"This pair only holds {amount(depth, r.pool.quote.symbol)}, less than the largest "
                           f"simulated trade. Most of this token's liquidity may be on another DEX.")

    s = r.static
    if s.upgradeable_proxy:
        medium.append("The token is an upgradeable proxy: its rules can be changed after you buy.")
    owner_active = s.ownership_renounced is False
    for sig, why in s.risky.items():
        (high if owner_active and "blacklist" in why else medium).append(
            f"Contract has {sig.split('(')[0]}(): {why}" + (" (owner still active)" if owner_active else ""))

    h = r.hook
    if h and h.upgradeable:
        medium.append("The pool's hook is upgradeable: whoever controls it can change swap behaviour later.")

    r.reasons = high + medium
    r.verdict = HIGH if high else MEDIUM if medium else LOW
    return r


def render_text(r: ScanReport) -> str:
    t = r.token
    lines = [f"Chain      {r.chain}",
             f"Token      {t.address}",
             # repr() so control characters or fake formatting in attacker-chosen names stay visible
             f"Name       {t.name!r} ({t.symbol!r})"]
    s = r.static
    if s.is_minimal_proxy:
        lines.append(f"Template   clone of {s.implementation}")
    elif s.upgradeable_proxy:
        lines.append(f"Proxy      upgradeable, implementation {s.implementation}")
    if s.owner:
        lines.append(f"Owner      {s.owner}" + (" (renounced)" if s.ownership_renounced else ""))
    for label, p in [("Pool      ", r.pool)] * bool(r.pool) + [("Other pool", p) for p in r.other_pools]:
        if isinstance(p, Pool):
            extra = f"hook {p.hooks}" if p is r.pool else f"liquidity {p.liquidity:,}"
            lines.append(f"{label} v4 {p.pool_id[:18]}...  fee {p.fee / 10_000:g}%  {extra}")
        else:
            depth = amount(p.quote_reserve / 10 ** p.quote.decimals, p.quote.symbol)
            lines.append(f"{label} v2 {p.pair}  {p.quote.symbol}  fee {p.fee_bps / 100:g}%  depth {depth}")
    if r.hook:
        lines.append(f"Hook perms {', '.join(r.hook.permissions)}")
    if r.trips:
        lines.append("")
        lines.append("Buy-then-sell simulation (eth_call, nothing broadcast):")
        for trip in r.trips:
            size = amount(trip.size, trip.quote_symbol)
            if not trip.buy_ok or not trip.sell_ok:
                lines.append(f"  {size:>12}  FAILED  {trip.error}")
                continue
            gas = gas_amount(gas_cost(trip.buy_gas + trip.sell_gas))
            back = amount(trip.quote_received / trip.quote_paid * trip.size, trip.quote_symbol)
            lines.append(f"  {size:>12}  -> {back} back  (loss {trip.round_trip_loss:.1%}, gas {gas})")
    lines.append("")
    lines.append(f"RISK: {r.verdict}")
    lines.extend(f"  - {reason}" for reason in r.reasons)
    lines.extend(f"  note: {note}" for note in r.notes)
    lines.append("\nRisk signals, not a guarantee. A token can change after this scan.")
    return "\n".join(lines)
