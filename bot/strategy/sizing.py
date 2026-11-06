from bot.strategy.learner import learner

# Полная загрузка капитала: увеличены базовые и максимальные чеки на сделку
TIERS = [
    (150, 22, 40),
    (250, 35, 65),
    (500, 48, 100),
    (1000, 65, 150),
    (2500, 120, 350),
    (5000, 220, 700),
    (10000, 400, 1400),
]

TIER_OFFSET_BOUNDS = {
    "TOP20": (-0.0035, -0.0012),
    "MID":   (-0.0052, -0.0018),
    "SMALL": (-0.0075, -0.0024),
    "MICRO": (-0.0115, -0.0038),
}

TIER_ACCUM_BOUNDS = {
    "TOP20": (-0.0024, -0.0010),
    "MID":   (-0.0036, -0.0014),
    "SMALL": (-0.0050, -0.0020),
    "MICRO": (-0.0075, -0.0030),
}

TIER_ROCKET_BOUNDS = {
    "TOP20": (-0.0022, -0.0008),
    "MID":   (-0.0035, -0.0012),
    "SMALL": (-0.0048, -0.0018),
    "MICRO": (-0.0070, -0.0028),
}

TIER_REVERSAL_BOUNDS = {
    "TOP20": (-0.0045, -0.0015),
    "MID":   (-0.0065, -0.0020),
    "SMALL": (-0.0088, -0.0028),
    "MICRO": (-0.0120, -0.0040),
}


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(400.0, equity * 0.05), equity * 0.15


def min_order_threshold(equity: float) -> float:
    base_min, _ = tier_limits(equity)
    return max(18.0, round(base_min * 0.75, 2))


def portfolio_limits(equity):
    """Свободные лимиты по секторам, чтобы капитал не простаивал в кэше."""
    if equity <= 150:
        return 4, 4
    if equity <= 250:
        return 5, 5
    if equity <= 500:
        return 7, 6
    if equity <= 1000:
        return 10, 8
    if equity <= 2500:
        return 12, 10
    return 15, 12


def buy_size(equity, score, thr, liquidity, free_usdt, entry_mode="sniper", grade="B"):
    base_min, base_max = tier_limits(equity)
    score_range = 10.0 - thr
    strength = 0.0 if score_range <= 0 else max(0.0, min(1.0, (score - thr) / score_range))

    size = base_min + (base_max - base_min) * strength
    max_allowed = base_max

    if liquidity < 500_000:
        size = base_min * 0.85

    kelly = learner.kelly_multiplier(entry_mode)
    if kelly is not None:
        size *= (0.75 + 0.35 * kelly)

    # Полновесный сайзинг: [💎] A+ получает 1.10x..1.20x, а рабочий [B] — 0.90x (без урезания на треть)
    if grade == "A+":
        size *= max(1.10, learner.grade_a_trust_mult())
    else:
        size *= 0.90

    min_allowed = min_order_threshold(equity)
    size = max(min_allowed, min(size, max_allowed))
    size = min(size, free_usdt * 0.98, equity * 0.16)

    return round(size, 2)


def entry_offset(score, thr, regime, atr_pct, entry_mode="sniper", tier="SMALL", is_accumulation=False):
    from bot.strategy.shadow import shadow
    hunt = shadow.hunt()
    tier = tier or "SMALL"
    surplus = max(0.0, score - thr)
    strength_alpha = min(1.0, surplus / 2.5)

    if entry_mode == "rocket":
        bounds = TIER_ROCKET_BOUNDS.get(tier, TIER_ROCKET_BOUNDS["SMALL"])
        raw = -atr_pct / 100 * 0.20 * (1.0 - strength_alpha * 0.45)
        return max(bounds[0], min(bounds[1], raw))

    if entry_mode == "reversal":
        bounds = TIER_REVERSAL_BOUNDS.get(tier, TIER_REVERSAL_BOUNDS["SMALL"])
        raw = -atr_pct / 100 * 0.28 * (1.0 - strength_alpha * 0.35)
        return max(bounds[0], min(bounds[1], raw))

    if is_accumulation:
        bounds = TIER_ACCUM_BOUNDS.get(tier, TIER_ACCUM_BOUNDS["SMALL"])
        base_accum_target = -atr_pct / 100 * 0.20
        target = base_accum_target * (1.0 - strength_alpha * 0.45)
        if regime == "bear":
            target *= 1.10
        return max(bounds[0], min(bounds[1], target))

    bounds = TIER_OFFSET_BOUNDS.get(tier, TIER_OFFSET_BOUNDS["SMALL"])
    base_pullback = -atr_pct / 100 * 0.32

    if regime == "bear":
        target = min(hunt * 1.05, base_pullback * 1.05)
    elif regime == "bull":
        target = min(shadow.near(), base_pullback * 0.70)
    else:
        pullback_mult = 0.65 if strength_alpha >= 0.7 else (0.80 if strength_alpha >= 0.3 else 1.0)
        target = min(hunt * pullback_mult, base_pullback * pullback_mult)

    clamped_target = max(bounds[0], min(bounds[1], target))
    dynamic_offset = clamped_target + (bounds[1] - clamped_target) * (strength_alpha * 0.60)

    return max(bounds[0], min(bounds[1], dynamic_offset))
