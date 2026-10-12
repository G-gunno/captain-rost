from bot.strategy.indicators import rsi


def detect_patterns(candles_15m: list, a15: float, e50: float, e21: float, recent_high: float) -> list:
    """
    Price Action & Liquidity Pattern Engine.
    Строгая контекстная валидация моделей:
    1. pinbar -> reversal (на дне/панике) ИЛИ sniper (отскок по тренду)
    2. divergence -> reversal (бычья дивергенция RSI)
    3. vcp -> sniper (сжатие волатильности строго в аптренде e21 >= e50)
    4. breakout -> rocket (истинный пробой хая предыдущих свечей)
    5. engulfing -> sniper/reversal (поглощение)
    """
    if not candles_15m or len(candles_15m) < 25:
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
    r = rsi(closes)

    # 1. SPRING PINBAR (Контекстный пин-бар)
    if (lower_wick / c_range) >= 0.45 and (last["close"] >= last["low"] + 0.45 * c_range) and vol_ratio >= 1.2:
        # На дне рынка (перепроданность или под EMA50) — это реверсал
        if r <= 42 or last["close"] < e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Пин-бар выкупа на дне (хвост {lower_wick/c_range*100:.0f}%, vol x{vol_ratio:.1f})",
                "mode": "reversal",
                "base_bonus": 3.0
            })
        # В аптренде над EMA50 — это подтвержденный снайперский отскок
        elif e21 >= e50 and last["close"] >= e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Молот по тренду (хвост {lower_wick/c_range*100:.0f}%, vol x{vol_ratio:.1f})",
                "mode": "sniper",
                "base_bonus": 2.0
            })

    # 2. BULLISH DIVERGENCE (Бычья дивергенция RSI)
    rsi_now = r
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

    # 3. VCP COMPRESSION (Сжатие волатильности СТРОГО в аптренде)
    if e50 > 0 and e21 >= (e50 * 0.999):
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

    # 4. CLEAN BREAKOUT (Истинный пробой ПРЕДЫДУЩЕГО максимума)
    if recent_high > 0 and last["close"] > recent_high:
        solid_body = (body / c_range) >= 0.55 if c_range > 0 else False
        clean_top = (upper_wick / c_range) <= 0.20 if c_range > 0 else False
        if is_green and solid_body and clean_top and vol_ratio >= 1.35:
            patterns.append({
                "key": "breakout",
                "name": f"Истинный пробой хая ${recent_high:.4f} (vol x{vol_ratio:.1f})",
                "mode": "rocket",
                "base_bonus": 3.0
            })

    # 5. FAILED AUCTION (Бычье поглощение)
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
