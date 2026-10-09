# Формат: (Equity, Floor (Base Min), Ceiling (Base Max))
TIERS = [
    (150, 10, 25),
    (250, 15, 40),
    (500, 20, 75),
    (1000, 30, 120),
    (2500, 50, 250),
    (5000, 100, 500),
    (10000, 200, 1000),
]

# Границы отступа для стандартного отката (Sniper Pullback)
TIER_OFFSET_BOUNDS = {
    "TOP20": (-0.0035, -0.0010),  # 🐋 Киты: -0.10% ... -0.35% (высокая ликвидность, спред)
    "MID":   (-0.0065, -0.0020),  # 🐘 Слоны: -0.20% ... -0.65%
    "SMALL": (-0.0100, -0.0035),  # 🐅 Тигры: -0.35% ... -1.00% (умеренный откат)
    "MICRO": (-0.0160, -0.0060),  # 🐭 Мыши: -0.60% ... -1.60% (ловля теней в тонком стакане)
}

# Специальные границы для паттерна Накопления (Тихая консолидация на EMA50)
TIER_ACCUM_BOUNDS = {
    "TOP20": (-0.0015, -0.0005),  # 🐋 Киты: в спред / -0.05% ... -0.15%
    "MID":   (-0.0030, -0.0010),  # 🐘 Слоны: -0.10% ... -0.30%
    "SMALL": (-0.0045, -0.0015),  # 🐅 Тигры: -0.15% ... -0.45% (чтобы не упустить памп из сжатия!)
    "MICRO": (-0.0065, -0.0025),  # 🐭 Мыши: -0.25% ... -0.65%
}

# Границы для импульсных ракет (Momentum / Rocket)
TIER_ROCKET_BOUNDS = {
    "TOP20": (-0.0020, -0.0005),  # -0.05% ... -0.20%
    "MID":   (-0.0035, -0.0010),  # -0.10% ... -0.35%
    "SMALL": (-0.0050, -0.0020),  # -0.20% ... -0.50%
    "MICRO": (-0.0080, -0.0035),  # -0.35% ... -0.80%
}

def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(200.0, equity * 0.02), equity * 0.10

def portfolio_limits(equity):
    if equity <= 150: return 2, 2
    if equity <= 250: return 3, 2
    if equity <= 500: return 4, 3
    if equity <= 1000: return 6, 8
    if equity <= 2500: return 8, 10
    if equity <= 5000: return 10, 12
    return 12, 15

def buy_size(equity, score, thr, liquidity, free_usdt, kind="core", entry_mode="sniper"):
    base_min, base_max = tier_limits(equity)
    
    if kind == "satellite":
        sat_max = base_min * 3.0
        score_range = 10.0 - thr
        if score_range <= 0:
            strength = 0.0
        else:
            strength = max(0.0, min(1.0, (score - thr) / score_range))
        size = base_min + (sat_max - base_min) * strength
    else:
        score_range = 10.0 - thr
        if score_range <= 0:
            strength = 0.0
        else:
            strength = max(0.0, min(1.0, (score - thr) / score_range))
        size = base_min + (base_max - base_min) * strength

    if liquidity < 500_000:
        size = base_min

    max_allowed = (base_min * 3.0) if kind == "satellite" else base_max
    size = max(base_min, min(size, max_allowed))
    size = min(size, free_usdt * 0.95, equity * 0.20)
    
    return round(size, 2)

def entry_offset(score, thr, regime, atr_pct, entry_mode="sniper", tier="SMALL", is_accumulation=False):
    """
    Умный расчет отступа лимитного ордера с адаптацией под:
    - Весовую категорию (Whales, Elephants, Tigers, Mice)
    - Паттерн накопления (не ставить ордер в бездну, когда пружина сжата)
    - Силу сигнала (Surplus): чем мощнее сетап, тем ближе к текущей цене ставится ордер.
    """
    from bot.strategy.shadow import shadow 
    hunt = shadow.hunt() 
    tier = tier or "SMALL"
    surplus = max(0.0, score - thr)
    
    # Нормализация силы сигнала от 0.0 (на грани порога) до 1.0 (сильнейший сетап >= +2.5 балла)
    strength_alpha = min(1.0, surplus / 2.5)

    # 1. РАКЕТА (Momentum): бьем вплотную к цене
    if entry_mode == "rocket":
        bounds = TIER_ROCKET_BOUNDS.get(tier, TIER_ROCKET_BOUNDS["SMALL"])
        raw = -atr_pct / 100 * 0.20 * (1.0 - strength_alpha * 0.4)
        return max(bounds[0], min(bounds[1], raw))

    # 2. РЕВЕРСАЛ (Ловец дна): вход в спред на отскоке
    if entry_mode == "reversal":
        return max(0.0, min(0.0030, atr_pct / 100 * 0.15))

    # 3. НАКОПЛЕНИЕ (Консолидация на EMA50):
    # Пружина уже сжата, глубокого отката не будет! Ставим ордер вплотную к сжатию.
    if is_accumulation:
        bounds = TIER_ACCUM_BOUNDS.get(tier, TIER_ACCUM_BOUNDS["SMALL"])
        base_accum_target = -atr_pct / 100 * 0.20
        # Чем выше балл, тем ближе прижимаем к верхней границе
        target = base_accum_target * (1.0 - strength_alpha * 0.5)
        if regime == "bear":
            target *= 1.2
        return max(bounds[0], min(bounds[1], target))

    # 4. СТАНДАРТНЫЙ СНАЙПЕР (Pullback по тренду):
    bounds = TIER_OFFSET_BOUNDS.get(tier, TIER_OFFSET_BOUNDS["SMALL"])
    base_pullback = -atr_pct / 100 * 0.45

    if regime == "bear":
        target = min(hunt * 1.3, base_pullback * 1.3)
    elif regime == "bull":
        target = min(shadow.near(), base_pullback * 0.7)
    else:
        # В нейтральном рынке прижимаем лимитку ближе, если сигнал сильный
        pullback_mult = 0.6 if strength_alpha >= 0.7 else (0.85 if strength_alpha >= 0.3 else 1.1)
        target = min(hunt * pullback_mult, base_pullback * pullback_mult)

    # Интерполяция между нижней и верхней планкой на основе силы сигнала
    clamped_target = max(bounds[0], min(bounds[1], target))
    dynamic_offset = clamped_target + (bounds[1] - clamped_target) * (strength_alpha * 0.6)
    
    return max(bounds[0], min(bounds[1], dynamic_offset))
