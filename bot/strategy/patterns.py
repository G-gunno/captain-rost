from bot.strategy.indicators import rsi


def detect_patterns(candles_15m: list, a15: float, e50: float, e21: float, recent_high: float) -> list:
    """
    Price Action & Liquidity Pattern Engine.
    Защищен от шума незакрытых свечей (проверяет закрытый бар [-2] + подтверждение на [-1]):
    1. pinbar -> reversal (на дне/панике) ИЛИ sniper (молот по тренду)
    2. divergence -> reversal (бычья дивергенция RSI с разворотом цены)
    3. vcp -> sniper (сжатие волатильности на сухих ЗАКРЫТЫХ свечах в аптренде)
    4. breakout -> rocket (истинный пробой хая предыдущих свечей на объеме)
    5. engulfing -> sniper (бычье поглощение / ловушка продавцов)
    """
    if not candles_15m or len(candles_15m) < 25:
        return []

    patterns = []
    curr = candles_15m[-1]   # Текущая (формирующаяся) свеча
    closed = candles_15m[-2] # Последняя полностью закрытая 15m свеча
    prev2 = candles_15m[-3]  # Предшествующая закрытая свеча
    closes = [c["close"] for c in candles_15m]

    vols = [c["volume"] for c in candles_15m]
    avg_vol = sum(vols[-22:-2]) / 20 if len(vols) >= 22 else (sum(vols) / max(1, len(vols)))
    vol_ratio_curr = (curr["volume"] / avg_vol) if avg_vol > 0 else 1.0
    vol_ratio_closed = (closed["volume"] / avg_vol) if avg_vol > 0 else 1.0
    r = rsi(closes)

    # =========================================================================
    # 1. SPRING PINBAR (Контекстный пин-бар: закрытая свеча [-2] ИЛИ мощная [-1])
    # =========================================================================
    pinbar_found = False
    pin_wick_pct = 0.0
    pin_vol = 1.0

    # Проверка по закрытой свече [-2] с подтверждением, что [-1] не пробила дно
    closed_range = closed["high"] - closed["low"]
    if closed_range > 0:
        closed_lwick = min(closed["open"], closed["close"]) - closed["low"]
        if (closed_lwick / closed_range) >= 0.45 and (closed["close"] >= closed["low"] + 0.45 * closed_range) and vol_ratio_closed >= 1.15:
            if curr["close"] >= closed["low"] + 0.35 * closed_range:
                pinbar_found = True
                pin_wick_pct = (closed_lwick / closed_range) * 100
                pin_vol = vol_ratio_closed

    # Проверка по текущей свече [-1], если на ней уже прошел сильный всплеск объема
    curr_range = curr["high"] - curr["low"]
    if not pinbar_found and curr_range > 0:
        curr_lwick = min(curr["open"], curr["close"]) - curr["low"]
        if (curr_lwick / curr_range) >= 0.45 and (curr["close"] >= curr["low"] + 0.50 * curr_range) and vol_ratio_curr >= 1.35:
            pinbar_found = True
            pin_wick_pct = (curr_lwick / curr_range) * 100
            pin_vol = vol_ratio_curr

    if pinbar_found:
        if r <= 42 or curr["close"] < e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Пин-бар выкупа на дне (хвост {pin_wick_pct:.0f}%, vol x{pin_vol:.1f})",
                "mode": "reversal",
                "base_bonus": 3.0
            })
        elif e21 >= e50 and curr["close"] >= e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Молот по тренду (хвост {pin_wick_pct:.0f}%, vol x{pin_vol:.1f})",
                "mode": "sniper",
                "base_bonus": 2.0
            })

    # =========================================================================
    # 2. BULLISH DIVERGENCE (Бычья дивергенция RSI + начало откупа)
    # =========================================================================
    rsi_now = r
    rsi_past = rsi(closes[:-6])
    low_now = min(c["low"] for c in candles_15m[-6:])
    low_past = min(c["low"] for c in candles_15m[-18:-6])
    turning_up = curr["close"] >= curr["open"] or curr["close"] > closed["close"]

    if low_now < low_past and rsi_now > (rsi_past + 2.5) and rsi_now <= 45 and turning_up:
        patterns.append({
            "key": "divergence",
            "name": f"Бычья дивергенция RSI ({rsi_past:.0f} -> {rsi_now:.0f})",
            "mode": "reversal",
            "base_bonus": 2.5
        })

    # =========================================================================
    # 3. VCP COMPRESSION (Сжатие волатильности по закрытым свечам в аптренде)
    # =========================================================================
    if e50 > 0 and e21 >= (e50 * 0.998):
        is_near_ema50 = 0.996 <= (curr["close"] / e50) <= 1.014
        recent_box = (max(c["high"] for c in candles_15m[-5:]) - min(c["low"] for c in candles_15m[-5:])) / curr["close"]
        
        # Считаем средний объем строго по 4 последним ЗАКРЫТЫМ свечам (исключая 1-ю минуту новой свечи)
        closed_4_vol = sum(c["volume"] for c in candles_15m[-5:-1]) / 4
        vcp_vol_ratio = (closed_4_vol / avg_vol) if avg_vol > 0 else 1.0
        is_dry_volume = vcp_vol_ratio <= 1.15

        curr_body = abs(curr["close"] - curr["open"])
        not_dump = not (curr["close"] < curr["open"] and curr_body > 0.45 * a15)

        if is_near_ema50 and recent_box <= 0.022 and is_dry_volume and not_dump:
            patterns.append({
                "key": "vcp",
                "name": f"Сжатие VCP на EMA50 (бокс {recent_box*100:.1f}%, vol x{vcp_vol_ratio:.1f})",
                "mode": "sniper",
                "base_bonus": 3.0
            })

    # =========================================================================
    # 4. CLEAN BREAKOUT (Истинный пробой ПРЕДЫДУЩЕГО максимума)
    # =========================================================================
    if recent_high > 0:
        # Вариант А: Пробой уже зафиксирован закрытой свечой [-2] и цена держится выше
        closed_green = closed["close"] > closed["open"]
        closed_body = abs(closed["close"] - closed["open"])
        closed_uwick = closed["high"] - max(closed["open"], closed["close"])
        closed_solid = (closed_body / closed_range) >= 0.55 if closed_range > 0 else False
        closed_clean = (closed_uwick / closed_range) <= 0.22 if closed_range > 0 else False

        brk_confirmed_closed = (
            closed["close"] > recent_high and closed_green and 
            closed_solid and closed_clean and vol_ratio_closed >= 1.30 and
            curr["close"] >= recent_high * 0.999
        )

        # Вариант Б: Мощный импульс на текущей свече с уже набранным объемом >= 1.4x
        curr_green = curr["close"] > curr["open"]
        curr_body = abs(curr["close"] - curr["open"])
        curr_uwick = curr["high"] - max(curr["open"], curr["close"])
        curr_solid = (curr_body / curr_range) >= 0.60 if curr_range > 0 else False
        curr_clean = (curr_uwick / curr_range) <= 0.20 if curr_range > 0 else False

        brk_live = (
            curr["close"] > recent_high and curr_green and 
            curr_solid and curr_clean and vol_ratio_curr >= 1.40
        )

        if brk_confirmed_closed or brk_live:
            b_vol = vol_ratio_curr if brk_live else vol_ratio_closed
            patterns.append({
                "key": "breakout",
                "name": f"Истинный пробой хая ${recent_high:.4f} (vol x{b_vol:.1f})",
                "mode": "rocket",
                "base_bonus": 3.0
            })

    # =========================================================================
    # 5. FAILED AUCTION (Бычье поглощение)
    # =========================================================================
    # Проверяем закрытую пару ([-3] красная, [-2] зеленая с поглощением) и удержание на [-1]
    closed_engulf = (
        prev2["close"] < prev2["open"] and
        closed["close"] > closed["open"] and
        closed["close"] >= prev2["open"] and
        closed["open"] <= prev2["close"] and
        closed["volume"] >= prev2["volume"] and
        curr["close"] >= closed["open"]
    )
    # Или живое поглощение с полным объемом
    live_engulf = (
        closed["close"] < closed["open"] and
        curr["close"] > curr["open"] and
        curr["close"] >= closed["open"] and
        curr["open"] <= closed["close"] and
        curr["volume"] >= closed["volume"] * 1.1
    )

    if closed_engulf or live_engulf:
        patterns.append({
            "key": "engulfing",
            "name": "Бычье поглощение (ловушка продавцов)",
            "mode": "sniper",
            "base_bonus": 2.0
        })

    return patterns
