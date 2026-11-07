from bot.strategy.learner import learner

# Максимум 3 открытые позиции из одного тира (TOP20, MID, SMALL, MICRO)
MAX_POSITIONS_PER_TIER = 3
# Умный Овербукинг: до 5 активных лимитных ордеров на 1 тир до наполнения 3 позиций
MAX_ORDERS_PER_TIER = 5

TIERS = [
    (150, 20, 42),
    (250, 32, 68),
    (500, 45, 98),
    (1000, 52, 140),
    (2500, 110, 340),
    (5000, 210, 680),
    (10000, 380, 1350),
]

TIER_OFFSET_BOUNDS = {
    "TOP20": (-0.0032, -0.0010),
    "MID":   (-0.0048, -0.0015),
    "SMALL": (-0.0068, -0.0020),
    "MICRO": (-0.0105, -0.0032),
}

TIER_ACCUM_BOUNDS = {
    "TOP20": (-0.0022, -0.0008),
    "MID":   (-0.0032, -0.0012),
    "SMALL": (-0.0045, -0.0018),
    "MICRO": (-0.0068, -0.0026),
}

TIER_ROCKET_BOUNDS = {
    "TOP20": (-0.0018, -0.0006),
    "MID":   (-0.0026, -0.0009),
    "SMALL": (-0.0036, -0.0012),
    "MICRO": (-0.0052, -0.0018),
}

TIER_REVERSAL_BOUNDS = {
    "TOP20": (-0.0042, -0.0014),
    "MID":   (-0.0060, -0.0018),
    "SMALL": (-0.0082, -0.0025),
    "MICRO": (-0.0115, -0.0036),
}


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(380.0, equity * 0.05), equity * 0.14


def min_order_threshold(equity: float) -> float:
    base_min, _ = tier_limits(equity)
    return max(18.0, round(base_min * 0.75, 2))


def portfolio_limits(equity):
    """Возвращает (лимит открытых позиций на 1 тир = 3, лимит овербукинга ордеров на 1 тир = 5)."""
    return MAX_POSITIONS_PER_TIER, MAX_ORDERS_PER_TIER


def buy_size(equity, score, thr, liquidity, free_usdt, entry_mode="sniper", grade="B"):
    base_min, base_max = tier_limits(equity)
    score_range = 10.0 - thr
    strength = 0.0 if score_range <= 0 else max(0.0, min(1.0, (score - thr) / score_range))

    size = base_min + (base_max - base_min) * (strength * 0.85)
    max_allowed = base_max

    if liquidity < 500_000:
        size = base_min * 0.85

    kelly = learner.kelly_multiplier(entry_mode)
    if kelly is not None:
        size *= (0.75 + 0.30 * kelly)

    # Умное разделение ставок: [💎] A+ получает повышенный рабочий объём (1.10x..1.20x),
    # а проходной сигнал [B] работает умеренным чеком (0.75x..0.82x), чтобы 1 победа A+ перекрывала 2 стопа B
    if grade == "A+":
        size *= max(1.10, min(1.22, learner.grade_a_trust_mult()))
    else:
        size *= (0.82 if strength >= 0.55 else 0.74)

    min_allowed = min_order_threshold(equity)
    size = max(min_allowed, min(size, max_allowed))
    size = min(size, free_usdt * 0.98, equity * 0.14)

    return round(size, 2)


def entry_offset(score, thr, regime, atr_pct, entry_mode="sniper", tier="SMALL", is_accumulation=False):
    from bot.strategy.shadow import shadow
    hunt = shadow.hunt()
    tier = tier or "SMALL"
    surplus = max(0.0, score - thr)
    strength_alpha = min(1.0, surplus / 2.5)

    if entry_mode == "rocket":
        bounds = TIER_ROCKET_BOUNDS.get(tier, TIER_ROCKET_BOUNDS["SMALL"])
        raw = -atr_pct / 100 * 0.16 * (1.0 - strength_alpha * 0.50)
        return max(bounds[0], min(bounds[1], raw))

    if entry_mode == "reversal":
        bounds = TIER_REVERSAL_BOUNDS.get(tier, TIER_REVERSAL_BOUNDS["SMALL"])
        raw = -atr_pct / 100 * 0.25 * (1.0 - strength_alpha * 0.35)
        return max(bounds[0], min(bounds[1], raw))

    if is_accumulation:
        bounds = TIER_ACCUM_BOUNDS.get(tier, TIER_ACCUM_BOUNDS["SMALL"])
        base_accum_target = -atr_pct / 100 * 0.18
        target = base_accum_target * (1.0 - strength_alpha * 0.45)
        if regime == "bear":
            target *= 1.10
        return max(bounds[0], min(bounds[1], target))

    bounds = TIER_OFFSET_BOUNDS.get(tier, TIER_OFFSET_BOUNDS["SMALL"])
    base_pullback = -atr_pct / 100 * 0.28

    if regime == "bear":
        target = min(hunt * 1.0, base_pullback * 1.0)
    elif regime == "bull":
        target = min(shadow.near(), base_pullback * 0.65)
    else:
        pullback_mult = 0.60 if strength_alpha >= 0.7 else (0.75 if strength_alpha >= 0.3 else 0.95)
        target = min(hunt * pullback_mult, base_pullback * pullback_mult)

    clamped_target = max(bounds[0], min(bounds[1], target))
    dynamic_offset = clamped_target + (bounds[1] - clamped_target) * (strength_alpha * 0.65)

    return max(bounds[0], min(bounds[1], dynamic_offset))
