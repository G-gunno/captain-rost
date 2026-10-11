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


def find_sr_levels(candles, window=48):
    """
    Поиск сильных локальных уровней поддержки и сопротивления.
    Уровень считается значимым ТОЛЬКО если подтвержден как минимум 2 касаниями.
    """
    if len(candles) < 20:
        return None, 0, None, 0

    recent = candles[-window:] if len(candles) >= window else candles
    last_price = recent[-1]["close"]
    
    swing_highs = []
    swing_lows = []
    
    n = len(recent)
    # Радиус 3 свечи (45 минут слева и справа) для исключения мелкого рыночного шума
    for i in range(3, n - 3):
        h = recent[i]["high"]
        l = recent[i]["low"]
        if all(h >= recent[i+j]["high"] for j in (-3, -2, -1, 1, 2, 3)):
            swing_highs.append(h)
        if all(l <= recent[i+j]["low"] for j in (-3, -2, -1, 1, 2, 3)):
            swing_lows.append(l)

    res_above = [h for h in swing_highs if h > last_price * 1.002]
    nearest_res = None
    res_touches = 0
    if res_above:
        # Ищем кластеры (уровни с наибольшим числом касаний)
        clusters = []
        for h in set(res_above):
            touches = sum(1 for x in res_above if abs(x - h) / h <= 0.005)
            clusters.append((h, touches))
        
        # Фильтруем только подтвержденные уровни (минимум 2 касания)
        confirmed_res = [c for c in clusters if c[1] >= 2]
        if confirmed_res:
            confirmed_res.sort(key=lambda x: x[0])  # Ближайшее подтвержденное сопротивление
            nearest_res, res_touches = confirmed_res[0]

    sup_below = [l for l in swing_lows if l < last_price * 0.998]
    nearest_sup = None
    sup_touches = 0
    if sup_below:
        clusters_sup = []
        for l in set(sup_below):
            touches = sum(1 for x in sup_below if abs(x - l) / l <= 0.005)
            clusters_sup.append((l, touches))
        
        confirmed_sup = [c for c in clusters_sup if c[1] >= 2]
        if confirmed_sup:
            confirmed_sup.sort(key=lambda x: x[0], reverse=True)  # Ближайшая подтвержденная поддержка
            nearest_sup, sup_touches = confirmed_sup[0]

    return nearest_res, res_touches, nearest_sup, sup_touches
