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
    Поиск локальных уровней поддержки и сопротивления (Swing Highs & Lows).
    Возвращает:
      nearest_res: ближайшее сопротивление выше последней цены (или None)
      res_touches: число касаний уровня сопротивления
      nearest_sup: ближайшая поддержка ниже последней цены (или None)
      sup_touches: число касаний уровня поддержки
    """
    if len(candles) < 15:
        return None, 0, None, 0

    recent = candles[-window:] if len(candles) >= window else candles
    last_price = recent[-1]["close"]
    
    swing_highs = []
    swing_lows = []
    
    n = len(recent)
    for i in range(2, n - 2):
        h = recent[i]["high"]
        l = recent[i]["low"]
        if h >= recent[i-1]["high"] and h >= recent[i-2]["high"] and h >= recent[i+1]["high"] and h >= recent[i+2]["high"]:
            swing_highs.append(h)
        if l <= recent[i-1]["low"] and l <= recent[i-2]["low"] and l <= recent[i+1]["low"] and l <= recent[i+2]["low"]:
            swing_lows.append(l)

    res_above = [h for h in swing_highs if h > last_price * 1.001]
    nearest_res = None
    res_touches = 0
    if res_above:
        nearest_res = min(res_above)
        res_touches = sum(1 for h in swing_highs if abs(h - nearest_res) / nearest_res <= 0.004)

    sup_below = [l for l in swing_lows if l < last_price * 0.999]
    nearest_sup = None
    sup_touches = 0
    if sup_below:
        nearest_sup = max(sup_below)
        sup_touches = sum(1 for l in swing_lows if abs(l - nearest_sup) / nearest_sup <= 0.004)

    return nearest_res, res_touches, nearest_sup, sup_touches
