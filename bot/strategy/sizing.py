TIERS = [
    (150, 5, 30),
    (250, 10, 50),
    (500, 15, 75),
    (1000, 25, 120),
    (2500, 40, 200),
    (5000, 75, 350),
    (10000, 125, 600),
]

RISK_PER_TRADE_PCT = 1.0
SAT_SIZE_PCT = 2.0   
SAT_RISK_PCT = 0.5

def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(150.0, equity * 0.008), equity * 0.02

def portfolio_limits(equity):
    if equity <= 150: return 2, 2
    if equity <= 250: return 3, 2
    if equity <= 500: return 4, 3
    if equity <= 1000: return 6, 8
    if equity <= 2500: return 8, 10
    if equity <= 5000: return 10, 12
    return 12, 15

def buy_size(equity, score, liquidity, free_usdt, sl_dist_pct=1.0,
             kind="core", km=None, sat_size_pct=2.0, size_multiplier=1.0):
    
    if kind == "satellite":
        size = equity * sat_size_pct / 100
        if sl_dist_pct > 0:
            size = min(size, equity * SAT_RISK_PCT / sl_dist_pct)
        size *= size_multiplier  # Умный множитель режима
        size = min(size, free_usdt * 0.95)
        return round(size, 2)

    lo, hi = tier_limits(equity)
    strength = max(0.0, min(1.0, (score - 5) / 3.5))
    size = lo + (hi - lo) * strength
    if liquidity < 500_000:
        size = lo
        
    if km is not None:
        size *= (0.5 + 0.5 * km)  # Раздельный критерий Келли
        
    if sl_dist_pct > 0:
        size_risk = equity * RISK_PER_TRADE_PCT / sl_dist_pct
        size = min(size, size_risk)
        
    size *= size_multiplier  
    size = min(size, free_usdt * 0.95, equity * 0.20)
    return round(size, 2)
