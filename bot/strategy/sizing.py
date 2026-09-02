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
SAT_SIZE_PCT = 2.0   # базовый размер сателлита (факт приходит через sat_size_pct)
SAT_RISK_PCT = 0.5


def tier_limits(equity):
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(150.0, equity * 0.008), equity * 0.02


def portfolio_limits(equity):
    """Адаптивные лимиты НА СЕКТОР (включая Other) в зависимости от баланса.
    Общее число позиций НЕ ограничено — только свободными средствами
    и размером позиции (buy_size) + лимитом сателлитов."""
    if equity <= 150:
        return 2, 2
    if equity <= 250:
        return 3, 2
    if equity <= 500:
        return 4, 3
    if equity <= 1000:
        return 6, 8
    if equity <= 2500:
        return 8, 10
    if equity <= 5000:
        return 10, 12
    return 12, 15


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
             kind="core", realized=None, sat_size_pct=2.0):
    # --- САТЕЛЛИТ: адаптивный размер (приходит из learner), широкий стоп ---
    if kind == "satellite":
        size = equity * sat_size_pct / 100
        if sl_dist_pct > 0:
            size = min(size, equity * SAT_RISK_PCT / sl_dist_pct)
        size = min(size, free_usdt * 0.95)
        return round(size, 2)

    # --- CORE: сетка по equity + сила сигнала + Kelly ---
    lo, hi = tier_limits(equity)
    strength = max(0.0, min(1.0, (score - 5) / 3.5))
    size = lo + (hi - lo) * strength
    if liquidity < 500_000:
        size = lo
    km = kelly_multiplier(realized or [])
    if km is not None:
        size *= (0.5 + 0.5 * km)
    if sl_dist_pct > 0:
        size_risk = equity * RISK_PER_TRADE_PCT / sl_dist_pct
        size = min(size, size_risk)
    size = min(size, free_usdt * 0.95, equity * 0.20)
    return round(size, 2)
