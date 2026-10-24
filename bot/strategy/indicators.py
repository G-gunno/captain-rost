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


def _dedup_levels(levels):
    if not levels:
        return []
    deduped = []
    for lvl in levels:
        match = next((d for d in deduped if abs(d[0] - lvl[0]) / lvl[0] <= 0.005), None)
        if match:
            if lvl[1] > match[1]:
                deduped.remove(match)
                deduped.append(lvl)
        else:
            deduped.append(lvl)
    return deduped


def _count_spaced_touches(price_target: float, points: list, tol: float = 0.005) -> int:
    if price_target <= 0:
        return 0
    matched = [pt for pt in points if abs(pt[0] - price_target) / price_target <= tol]
    if not matched:
        return 0

    touches_weight = 0
    last_idx_by_tf = {}
    has_1h_touch = False
    for item in sorted(matched, key=lambda x: (x[1], x[2])):
        p_val, tf_tag, idx, min_gap = item[0], item[1], item[2], item[3]
        w = item[4] if len(item) > 4 else 1
        prev_idx = last_idx_by_tf.get(tf_tag)
        if prev_idx is None or (idx - prev_idx) >= min_gap:
            touches_weight += w
            if tf_tag == "1h":
                has_1h_touch = True
            last_idx_by_tf[tf_tag] = idx

    # Жесткое правило: статус «Капитальной стены» (вес >= 4) дается ТОЛЬКО при наличии минимум 1 часового (1h) касания!
    if touches_weight >= 4 and not has_1h_touch:
        touches_weight = 3

    return touches_weight


def get_all_sr_levels(candles_15m, candles_1h=None, window_15m=120, window_1h=72):
    if not candles_15m or len(candles_15m) < 15:
        return [], []

    high_points = []
    low_points = []

    if candles_1h and isinstance(candles_1h, list) and len(candles_1h) >= 20:
        recent_1h = candles_1h[-window_1h:] if len(candles_1h) >= window_1h else candles_1h
        n1 = len(recent_1h)
        for i in range(2, n1 - 2):
            h = recent_1h[i]["high"]
            l = recent_1h[i]["low"]
            if all(h >= recent_1h[i + j]["high"] for j in (-2, -1, 1, 2)):
                high_points.append((h, "1h", i, 3, 2))
            if all(l <= recent_1h[i + j]["low"] for j in (-2, -1, 1, 2)):
                low_points.append((l, "1h", i, 3, 2))

    recent_15m = candles_15m[-window_15m:] if len(candles_15m) >= window_15m else candles_15m
    n15 = len(recent_15m)
    for i in range(3, n15 - 1):
        h = recent_15m[i]["high"]
        l = recent_15m[i]["low"]
        if all(h >= recent_15m[i - j]["high"] for j in (1, 2, 3)) and h >= recent_15m[i + 1]["high"]:
            high_points.append((h, "15m", i, 4, 1))
        if all(l <= recent_15m[i - j]["low"] for j in (1, 2, 3)) and l <= recent_15m[i + 1]["low"]:
            low_points.append((l, "15m", i, 4, 1))

    unique_highs = {pt[0] for pt in high_points if pt[0] > 0}
    clusters_res = []
    for h in unique_highs:
        touches = _count_spaced_touches(h, high_points, tol=0.005)
        if touches >= 2:
            clusters_res.append((h, touches))
    clusters_res.sort(key=lambda x: x[0])

    unique_lows = {pt[0] for pt in low_points if pt[0] > 0}
    clusters_sup = []
    for l in unique_lows:
        touches = _count_spaced_touches(l, low_points, tol=0.005)
        if touches >= 2:
            clusters_sup.append((l, touches))
    clusters_sup.sort(key=lambda x: x[0], reverse=True)

    return _dedup_levels(clusters_res), _dedup_levels(clusters_sup)


def find_sr_levels(candles_15m, candles_1h=None, window_15m=120, window_1h=72, window=None):
    if window is not None:
        window_15m = window

    if not candles_15m or len(candles_15m) < 15:
        return None, 0, None, 0

    last_price = candles_15m[-1]["close"]
    confirmed_res, confirmed_sup = get_all_sr_levels(candles_15m, candles_1h, window_15m, window_1h)

    res_above = [c for c in confirmed_res if c[0] > last_price * 1.0025]
    nearest_res = None
    res_touches = 0
    if res_above:
        res_above.sort(key=lambda x: x[0])
        nearest_res, res_touches = res_above[0]

    sup_below = [c for c in confirmed_sup if c[0] < last_price * 0.9985]
    nearest_sup = None
    sup_touches = 0
    if sup_below:
        sup_below.sort(key=lambda x: x[0], reverse=True)
        nearest_sup, sup_touches = sup_below[0]

    return nearest_res, res_touches, nearest_sup, sup_touches
