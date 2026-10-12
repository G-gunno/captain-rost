from bot.strategy.indicators import rsi


def detect_patterns(candles_15m: list, a15: float, e50: float, recent_high: float) -> list:
    """
    Price Action & Liquidity Pattern Engine.
    Распознает 5 ключевых крипто-сетапов ликвидности:
    1. pinbar (Спринг / Пин-бар выкупа на дне)
    2. divergence (Бычья дивергенция RSI)
    3. vcp (Сжатие волатильности на EMA50 по Вайкоффу)
    4. breakout (Истинный импульсный пробой полки)
    5. engulfing (Ловушка продавца / бычье поглощение)
    """
    if not candles_15m or len(candles_15m) < 15:
        return []

    patterns = []
    last = candles_15m[-1]
    prev = candles_15m[-2]
    closes = [c["close"] for c in candles_15m]

    c_range = last["high"] - last["low"]
    if c_range <= 0:
        return []

    is_green = last["close"] >= last["open"]
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])

    vols = [c["volume"] for c in candles_15m]
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else (sum(vols) / len(vols))
    vol_ratio = (last["volume"] / avg_vol) if avg_vol > 0 else 1.0

    # 1. SPRING PINBAR (Пин-бар выкупа на дне)
    if (lower_wick / c_range) >= 0.45 and (last["close"] >= last["low"] + 0.45 * c_range) and vol_ratio >= 1.2:
        patterns.append({
            "key": "pinbar",
            "name": f"Пин-бар выкупа (хвост {lower_wick/c_range*100:.0f}%, vol x{vol_ratio:.1f})",
            "mode": "reversal",
            "base_bonus": 3.0
        })

    # 2. BULLISH DIVERGENCE (Бычья дивергенция RSI)
    if len(candles_15m) >= 25:
        rsi_now = rsi(closes)
        rsi_past = rsi(closes[:-6])
        low_now = min(c["low"] for c in candles_15m[-6:])
        low_past = min(c["low"] for c in candles_15m[-18:-6])

        if low_now < low_past and rsi_now > (rsi_past + 2.5) and rsi_now <= 45:
            patterns.append({
                "key": "divergence",
                "name": f"Бычья дивергенция RSI ({rsi_past:.0f} -> {rsi_now:.0f})",
                "mode": "reversal",
                "base_bonus": 2.5
            })

    # 3. VCP COMPRESSION (Сжатие волатильности по Вайкоффу на EMA50)
    if e50 > 0 and len(candles_15m) >= 8:
        is_near_ema50 = 0.998 <= (last["close"] / e50) <= 1.012
        recent_box = (max(c["high"] for c in candles_15m[-5:]) - min(c["low"] for c in candles_15m[-5:])) / last["close"]
        is_dry_volume = vol_ratio <= 1.2
        not_dump = not (not is_green and body > 0.5 * a15)

        if is_near_ema50 and recent_box <= 0.022 and is_dry_volume and not_dump:
            patterns.append({
                "key": "vcp",
                "name": f"Сжатие VCP на EMA50 (бокс {recent_box*100:.1f}%)",
                "mode": "sniper",
                "base_bonus": 3.0
            })

    # 4. CLEAN BREAKOUT (Истинный импульсный пробой)
    if recent_high > 0 and last["close"] >= recent_high * 0.998:
        solid_body = (body / c_range) >= 0.55 if c_range > 0 else False
        clean_top = (upper_wick / c_range) <= 0.25 if c_range > 0 else False
        if is_green and solid_body and clean_top and vol_ratio >= 1.35:
            patterns.append({
                "key": "breakout",
                "name": f"Импульсный пробой (чистый бар, vol x{vol_ratio:.1f})",
                "mode": "rocket",
                "base_bonus": 3.0
            })

    # 5. FAILED AUCTION (Бычье поглощение / Ловушка продавца)
    prev_red = prev["close"] < prev["open"]
    if prev_red and is_green:
        engulfs_body = last["close"] >= prev["open"] and last["open"] <= prev["close"]
        if engulfs_body and last["volume"] >= prev["volume"]:
            patterns.append({
                "key": "engulfing",
                "name": "Бычье поглощение (ловушка продавцов)",
                "mode": "sniper",
                "base_bonus": 2.0
            })

    return patterns
