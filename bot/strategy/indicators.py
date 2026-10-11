def ema(values, period):
    if not values:
        return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(candles, period=14):
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / min(period, len(trs))
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def find_sr_levels(candles_15m, candles_1h=None, window_15m=60, window_1h=72):
    """
    Институциональный мультитаймфрейм поиск S/R:
    - 1H свечи (горизонт 3 суток) дают фундаментальные уровни старшего порядка.
    - 15m свечи (горизонт 15 часов) дают локальные подтверждения.
    """
    if not candles_15m or len(candles_15m) < 15:
        return None, 0, None, 0

    last_price = candles_15m[-1]["close"]
    swing_highs = []
    swing_lows = []

    # 1. Старший таймфрейм 1H (3 дня истории)
    if candles_1h and len(candles_1h) >= 20:
        recent_1h = candles_1h[-window_1h:] if len(candles_1h) >= window_1h else candles_1h
        n1 = len(recent_1h)
        for i in range(2, n1 - 2):
            h = recent_1h[i]["high"]
            l = recent_1h[i]["low"]
            if all(h >= recent_1h[i+j]["high"] for j in (-2, -1, 1, 2)):
                swing_highs.extend([h, h])  # 1H уровни имеют двойной вес
            if all(l <= recent_1h[i+j]["low"] for j in (-2, -1, 1, 2)):
                swing_lows.extend([l, l])

    # 2. Рабочий таймфрейм 15m (15 часов истории)
    recent_15m = candles_15m[-window_15m:] if len(candles_15m) >= window_15m else candles_15m
    n15 = len(recent_15m)
    for i in range(3, n15 - 3):
        h = recent_15m[i]["high"]
        l = recent_15m[i]["low"]
        if all(h >= recent_15m[i+j]["high"] for j in (-3, -2, -1, 1, 2, 3)):
            swing_highs.append(h)
        if all(l <= recent_15m[i+j]["low"] for j in (-3, -2, -1, 1, 2, 3)):
            swing_lows.append(l)

    # Кластеризация сопротивлений выше текущей цены
    res_above = [h for h in swing_highs if h > last_price * 1.0015]
    nearest_res = None
    res_touches = 0
    if res_above:
        clusters = []
        for h in set(res_above):
            touches = sum(1 for x in res_above if abs(x - h) / h <= 0.005)
            clusters.append((h, touches))
        confirmed_res = [c for c in clusters if c[1] >= 2]
        if confirmed_res:
            confirmed_res.sort(key=lambda x: x[0])  # Ближайшее сильное сопротивление
            nearest_res, res_touches = confirmed_res[0]

    # Кластеризация поддержек ниже текущей цены
    sup_below = [l for l in swing_lows if l < last_price * 0.9985]
    nearest_sup = None
    sup_touches = 0
    if sup_below:
        clusters_sup = []
        for l in set(sup_below):
            touches = sum(1 for x in sup_below if abs(x - l) / l <= 0.005)
            clusters_sup.append((l, touches))
        confirmed_sup = [c for c in clusters_sup if c[1] >= 2]
        if confirmed_sup:
            confirmed_sup.sort(key=lambda x: x[0], reverse=True)  # Ближайшая сильная поддержка
            nearest_sup, sup_touches = confirmed_sup[0]

    return nearest_res, res_touches, nearest_sup, sup_touches
