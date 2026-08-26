TIERS = [
    (150, 5, 30),
    (250, 10, 50),
    (500, 15, 75),
    (1000, 25, 120),
    (2500, 40, 200),
    (5000, 75, 350),
    (10000, 125, 600),
]

RISK_PER_TRADE_PCT = 1.0   # риск на сделку (при срабатывании SL) <= 1% от equity (Core)
SAT_SIZE_PCT = 2.0         # базовый размер сателлита: 2% от equity
SAT_RISK_PCT = 0.5         # риск сателлита при SL <= 0.5% от equity


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(150.0, equity * 0.008), equity * 0.02


def kelly_multiplier(realized):
    """Half-Kelly по истории сделок. None — мало данных (используем базовый размер)."""
    if len(realized) < 10:
        return None
    wins = [r["pnl"] for r in realized if r["pnl"] > 0]
    losses = [r["pnl"] for r in realized if r["pnl"] <= 0]
    if not wins or not losses:
        return None
    p = len(wins) / len(realized)
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    b = avg_win / avg_loss if avg_loss else 0
    if b <= 0:
        return 0.0
    kelly = (b * p - (1 - p)) / b
    return round(max(0.0, min(kelly * 0.5, 1.0)), 3)


def buy_size(equity, score, liquidity, free_usdt, sl_dist_pct=1.0,
             kind="core", realized=None):
    # --- САТЕЛЛИТ: маленькая позиция, широкий стоп ---
    if kind == "satellite":
        size = equity * SAT_SIZE_PCT / 100
        if sl_dist_pct > 0:
            size = min(size, equity * SAT_RISK_PCT / sl_dist_pct)
        size = min(size, free_usdt * 0.95)
        return round(size, 2)

    # --- CORE: сетка по equity + сила сигнала + Kelly ---
    lo, hi = tier_limits(equity)
    strength = max(0.0, min(1.0, (score - 4) / 4))
    size = lo + (hi - lo) * strength
    if liquidity < 500_000:
        size = lo
    km = kelly_multiplier(realized or [])
    if km is not None:
        size *= (0.5 + 0.5 * km)   # от 50% до 100% базового размера по Kelly
    if sl_dist_pct > 0:
        size_risk = equity * RISK_PER_TRADE_PCT / sl_dist_pct
        size = min(size, size_risk)
    size = min(size, free_usdt * 0.95, equity * 0.20)
    return round(size, 2)
