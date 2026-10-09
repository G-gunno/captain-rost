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

TIER_OFFSET_BOUNDS = {
    "TOP20": (-0.0040, -0.0015),  # -0.40% ... -0.15% (в стакане)
    "MID":   (-0.0080, -0.0030),  # -0.80% ... -0.30%
    "SMALL": (-0.0140, -0.0050),  # -1.40% ... -0.50%
    "MICRO": (-0.0250, -0.0100),  # -2.50% ... -1.00% (ловля сквизов)
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

def entry_offset(score, thr, regime, atr_pct, entry_mode="sniper", tier="SMALL"):
    from bot.strategy.shadow import shadow 
    hunt = shadow.hunt() 
    tier = tier or "SMALL"

    if entry_mode == "rocket":
        raw_rocket = -atr_pct / 100 * 0.25
        if tier == "TOP20":
            return max(-0.0025, raw_rocket)
        return max(-0.0080, raw_rocket)
        
    elif entry_mode == "reversal":
        return max(0.0, atr_pct / 100 * 0.15)

    base_pullback = -atr_pct / 100 * 0.5
    surplus = score - thr
    
    # 🐋 Киты (TOP20): лимитка всегда рядом с текущей ценой
    if tier == "TOP20":
        mult = 1.2 if regime == "bear" else (0.8 if surplus >= 1.5 else 1.0)
        return max(-0.0040, min(-0.0015, base_pullback * mult))

    # 🐘 Слоны (MID): умеренное расстояние
    if tier == "MID":
        mult = 1.3 if regime == "bear" else (0.8 if surplus >= 1.5 else 1.0)
        return max(-0.0080, min(-0.0030, base_pullback * mult))

    # 🐅 Тигры (SMALL) и 🐭 Мыши (MICRO)
    if regime == "bear":
        target = min(hunt * 1.5, base_pullback * 1.5)  
    elif regime == "neutral":
        target = min(hunt * 1.2, base_pullback * 1.2)  
    else:
        if surplus >= 3.0:
            target = min(shadow.near(), base_pullback * 0.5)  
        elif surplus >= 1.5:
            target = min(hunt * 0.5, base_pullback * 0.8)
        else:
            target = min(hunt, base_pullback)

    bounds = TIER_OFFSET_BOUNDS.get(tier, TIER_OFFSET_BOUNDS["SMALL"])
    return max(bounds[0], min(bounds[1], target))
