from bot.strategy.shadow import shadow
from bot.strategy.learner import learner

MAX_POSITIONS = 10
MAX_POSITIONS_PER_SECTOR = 3
MAX_POSITIONS_PER_TIER = 3
MAX_ORDERS_PER_TIER = 5
MAX_SHARE = 0.14
MIN_ORDER_USDT = 15.0
RESERVE = 0.10


def min_order_threshold(equity: float) -> float:
    if equity <= 0:
        return MIN_ORDER_USDT
    return round(min(25.0, max(MIN_ORDER_USDT, equity * 0.015)), 2)


def buy_size(equity, score, thr, liquidity=10_000_000, free_usdt=None, entry_mode="sniper", grade="B", symbol: str = None):
    strength = min(1.0, max(0.25, (score - thr + 1.0) / 3.0))
    share = MAX_SHARE * (0.55 + 0.45 * strength)

    if grade == "A+":
        trust_mult = learner.grade_a_trust_mult()
        share *= (1.18 * trust_mult)
    else:
        if entry_mode in ("reversal", "rocket"):
            share *= 0.85

    if symbol:
        share *= learner.coin_trust_mult(symbol)

    kelly = learner.kelly_multiplier(entry_mode)
    if kelly is not None:
        if kelly <= 0.05:
            share *= 0.65
        else:
            kelly_share = MAX_SHARE * (0.5 + kelly)
            share = (share * 0.6) + (kelly_share * 0.4)

    target = equity * share

    if liquidity < 1_000_000:
        target *= 0.5
    elif liquidity < 3_000_000:
        target *= 0.75

    cap = equity * (1 - RESERVE) if free_usdt is None else max(0.0, free_usdt - equity * RESERVE)
    min_thr = min_order_threshold(equity)
    if cap < min_thr and free_usdt is not None and free_usdt >= min_thr:
        cap = free_usdt * 0.95

    size = min(target, cap, equity * MAX_SHARE)
    return round(size, 2) if size >= min_thr else 0.0


def entry_offset(score, thr, regime="neutral", atr_pct=1.5, entry_mode="sniper", tier="SMALL", is_accumulation=False, symbol: str = None):
    """
    Плотная постановка лимитки, чтобы не упускать пампы из-за недохода в 0.1% (фикс PENGU, INJ, GLMR):
    - rocket: -0.08% .. -0.15%
    - reversal / дно просадки: -0.15% .. -0.25%
    - sniper по тренду / VCP: -0.15% .. -0.42%
    """
    if entry_mode == "rocket":
        if tier in ("TOP20", "MID"):
            return -0.0008
        return -0.0015

    if entry_mode == "reversal":
        if tier in ("TOP20", "MID"):
            return -0.0015
        return -0.0024

    dna = shadow.get_coin_dna(symbol) if symbol else {"pullback_pct": 0.60}
    dna_pull_frac = max(0.0016, min(0.0045, float(dna.get("pullback_pct", 0.60)) / 100.0 * 0.30))

    hunt = shadow.hunt()
    near = shadow.near()
    strength = min(1.0, max(0.0, (score - thr) / 3.0))
    base_offset = hunt + (near - hunt) * strength
    base_offset = 0.65 * base_offset - 0.35 * dna_pull_frac

    if is_accumulation and entry_mode == "sniper":
        if tier in ("TOP20", "MID"):
            return round(max(-0.0022, min(-0.0014, base_offset * 0.68)), 4)
        return round(max(-0.0028, min(-0.0018, base_offset * 0.75)), 4)

    vol_factor = min(1.20, max(0.75, atr_pct / 1.5))
    if tier in ("TOP20", "MID"):
        vol_factor *= 0.80

    final_offset = base_offset * vol_factor
    return round(max(-0.0042, min(-0.0015, final_offset)), 4)
