TIERS = [
    (150, 5, 30),
    (250, 10, 50),
    (500, 15, 75),
    (1000, 25, 120),
    (2500, 40, 200),
    (5000, 75, 350),
    (10000, 125, 600),
]


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(150.0, equity * 0.008), equity * 0.02


def buy_size(equity, score, liquidity, free_usdt):
    lo, hi = tier_limits(equity)
    strength = max(0.0, min(1.0, (score - 4) / 4))
    size = lo + (hi - lo) * strength
    if liquidity < 500_000:      # низколиквидные -> нижняя граница
        size = lo
    size = min(size, free_usdt * 0.95, equity * 0.20)
    return round(size, 2)
