TIERS = [
    (150, 5, 30),
    (250, 10, 50),
    (500, 15, 75),
    (1000, 25, 120),
    (2500, 40, 200),
    (5000, 75, 350),
    (10000, 125, 600),
]

RISK_PER_TRADE_PCT = 1.0   # риск на сделку (при срабатывании SL) <= 1% от equity


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(150.0, equity * 0.008), equity * 0.02


def buy_size(equity, score, liquidity, free_usdt, sl_dist_pct=1.0):
    lo, hi = tier_limits(equity)
    strength = max(0.0, min(1.0, (score - 4) / 4))
    size = lo + (hi - lo) * strength
    if liquidity < 500_000:      # низколиквидные -> нижняя граница
        size = lo
    if sl_dist_pct > 0:          # широкий SL -> меньше позиция
        size_risk = equity * RISK_PER_TRADE_PCT / sl_dist_pct
        size = min(size, size_risk)
    size = min(size, free_usdt * 0.95, equity * 0.20)
    return round(size, 2)
