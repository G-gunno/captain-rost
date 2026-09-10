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

def buy_size(equity, score, thr, liquidity, free_usdt, kind="core", entry_mode="sniper", size_multiplier=1.0):
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

    # --- ИСПРАВЛЕНИЕ: Сайзинг по режимам ---
    if entry_mode == "rocket":
        size = size * size_multiplier
    elif entry_mode == "reversal":
        # Защита от падающих ножей: урезаем на 30%
        size = size * 0.7 * size_multiplier
    else:
        size = size * 1.1

    max_allowed = (base_min * 3.0) if kind == "satellite" else base_max
    size = max(base_min, min(size, max_allowed))
    size = min(size, free_usdt * 0.95, equity * 0.20)
    
    return round(size, 2)

# Добавь этот импорт в начало sizing.py, если его нет:
# from bot.strategy.shadow import shadow

def entry_offset(score, thr, regime, atr_pct, entry_mode="sniper"):
    from bot.strategy.shadow import shadow # Ленивый импорт во избежание цикличности
    hunt = shadow.hunt() 

    if entry_mode == "rocket":
        return max(shadow.capture(), atr_pct / 100 * 0.1)
    elif entry_mode == "reversal":
        return max(0.0, atr_pct / 100 * 0.15)

    base_pullback = -atr_pct / 100 * 0.5 
    surplus = score - thr
    
    if regime == "bear":
        return min(hunt * 1.5, base_pullback * 1.5)  
    elif regime == "neutral":
        return min(hunt * 1.2, base_pullback * 1.2)  

    if surplus >= 3.0:
        return min(shadow.near(), base_pullback * 0.5)  
    if surplus >= 1.5:
        return min(hunt * 0.5, base_pullback * 0.8)

    return min(hunt, base_pullback)
