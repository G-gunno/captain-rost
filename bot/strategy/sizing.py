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
    Рассчитывает глубину лимитного ордера:
    - rocket: в упор к стакану (-0.10% .. -0.18%)
    - reversal / откуп дна в белых кружках: плотный вход (-0.18% .. -0.32%), чтобы гарантированно забирать разворот со дна!
    - sniper по тренду: откат (-0.22% .. -0.55%)
    """
    if entry_mode == "rocket":
        if tier in ("TOP20", "MID"):
            return -0.0010
        return -0.0018

    # На подтверждённом дне (reversal) не ставим лимитку на -0.8% в воздух (из-за чего INJ и OPG улетали без нас),
    # а берём плотный ретест свечи разворота (-0.18% .. -0.32%)!
    if entry_mode == "reversal":
        if tier in ("TOP20", "MID"):
            return -0.0018
        return -0.0028

    dna = shadow.get_coin_dna(symbol) if symbol else {"pullback_pct": 0.65}
    dna_pull_frac = max(0.0020, min(0.0060, float(dna.get("pullback_pct", 0.65)) / 100.0 * 0.35))

    hunt = shadow.hunt()
    near = shadow.near()
    strength = min(1.0, max(0.0, (score - thr) / 3.0))
    base_offset = hunt + (near - hunt) * strength
    base_offset = 0.65 * base_offset - 0.35 * dna_pull_frac

    if is_accumulation and entry_mode == "sniper":
        if tier in ("TOP20", "MID"):
            return round(max(-0.0026, min(-0.0016, base_offset * 0.70)), 4)
        return round(max(-0.0034, min(-0.0020, base_offset * 0.80)), 4)

    vol_factor = min(1.25, max(0.75, atr_pct / 1.5))
    if tier in ("TOP20", "MID"):
        vol_factor *= 0.80

    final_offset = base_offset * vol_factor
    return round(max(-0.0055, min(-0.0018, final_offset)), 4)