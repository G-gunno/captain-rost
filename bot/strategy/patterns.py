from bot.strategy.indicators import rsi, ema


def detect_patterns(candles_15m: list, a15: float, e50: float, e21: float, recent_high: float) -> list:
    """
    Price Action & Liquidity Pattern Engine.
    Включает:
    1. Подтвержденный Spring Pinbar и W-базу (Second Test) с расчетом глубокой лимитки wick_entry_price.
    2. Истинный VCP (строго над EMA50 и растущей EMA21 с повышением минимумов — без ложных срабатываний на пробое вниз).
    3. Фильтр «истощения импульса» для rocket 🚀.
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
    # 1. SPRING PINBAR (с подтверждением остановки ножа и лимиткой в глубину тени)
    # =========================================================================
    pinbar_found = False
    pin_wick_pct = 0.0
    pin_vol = 1.0
    wick_entry_price = None

    closed_range = closed["high"] - closed["low"]
    if closed_range > 0:
        closed_body_low = min(closed["open"], closed["close"])
        closed_lwick = closed_body_low - closed["low"]
        # Требуем закрытия свечи в верхней половине диапазона и удержания уровня текущей свечой
        if (closed_lwick / closed_range) >= 0.48 and (closed["close"] >= closed["low"] + 0.52 * closed_range) and vol_ratio_closed >= 1.20:
            if curr["close"] >= closed_body_low and curr["low"] >= closed["low"] * 0.9985:
                pinbar_found = True
                pin_wick_pct = (closed_lwick / closed_range) * 100
                pin_vol = vol_ratio_closed
                # Целимся лимиткой на 45% вглубь тени от низа тела свечи (Second Test)
                wick_entry_price = round(closed_body_low - 0.45 * closed_lwick, 8)

    curr_range = curr["high"] - curr["low"]
    if not pinbar_found and curr_range > 0:
        curr_body_low = min(curr["open"], curr["close"])
        curr_lwick = curr_body_low - curr["low"]
        if (curr_lwick / curr_range) >= 0.50 and (curr["close"] >= curr["open"]) and (curr["close"] >= curr["low"] + 0.58 * curr_range) and vol_ratio_curr >= 1.45:
            pinbar_found = True
            pin_wick_pct = (curr_lwick / curr_range) * 100
            pin_vol = vol_ratio_curr
            wick_entry_price = round(curr_body_low - 0.45 * curr_lwick, 8)

    if pinbar_found:
        if r <= 42 or curr["close"] < e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Пин-бар выкупа на дне (хвост {pin_wick_pct:.0f}%, vol x{pin_vol:.1f})",
                "mode": "reversal",
                "base_bonus": 3.0,
                "wick_entry_price": wick_entry_price,
            })
        elif e21 >= e50 and curr["close"] >= e50:
            patterns.append({
                "key": "pinbar",
                "name": f"Молот по тренду (хвост {pin_wick_pct:.0f}%, vol x{pin_vol:.1f})",
                "mode": "sniper",
                "base_bonus": 2.0,
                "wick_entry_price": wick_entry_price,
            })

    # =========================================================================
    # 2. BULLISH DIVERGENCE (Бычья дивергенция RSI + подтвержденная W-база)
    # =========================================================================
    rsi_now = r
    rsi_past = rsi(closes[:-6])
    low_now = min(c["low"] for c in candles_15m[-6:])
    low_past = min(c["low"] for c in candles_15m[-18:-6])
    # Подтверждение разворота: закрытая зеленая свеча ИЛИ выход выше максимума предыдущей свечи
    turning_up = (closed["close"] > closed["open"] and curr["close"] >= closed["close"] * 0.999) or (curr["close"] > closed["high"])

    if low_now <= low_past * 1.003 and rsi_now > (rsi_past + 3.0) and rsi_now <= 46 and turning_up:
        w_base_entry = round(low_now + (curr["close"] - low_now) * 0.32, 8)
        patterns.append({
            "key": "divergence",
            "name": f"Бычья дивергенция RSI ({rsi_past:.0f} -> {rsi_now:.0f})",
            "mode": "reversal",
            "base_bonus": 2.6,
            "wick_entry_price": w_base_entry,
        })

    # =========================================================================
    # 3. ИСТИННЫЙ VCP COMPRESSION (Защита от пробоя EMA50 сверху вниз — кейс UNI/RECALL)
    # =========================================================================
    e21_series = ema(closes, 21)
    e21_rising = len(e21_series) >= 5 and e21_series[-1] >= e21_series[-4] * 0.9998

    if e50 > 0 and e21 > (e50 * 1.0005) and e21_rising:
        # Все 4 закрытые свечи бокса обязаны закрываться ВЫШЕ EMA50, а текущая цена — удерживать EMA21
        box_candles = candles_15m[-5:-1]
        all_closed_above_e50 = all(c["close"] >= e50 * 0.9995 for c in box_candles)
        curr_above_e21 = curr["close"] >= e21 * 0.9985
        is_near_ema = 1.000 <= (curr["close"] / e50) <= 1.016

        recent_box = (max(c["high"] for c in candles_15m[-5:]) - min(c["low"] for c in candles_15m[-5:])) / curr["close"]

        # Повышающиеся минимумы (Higher Lows — поджатие снизу вверх, а не сползание вниз)
        lows_recent = min(c["low"] for c in candles_15m[-3:])
        lows_prev = min(c["low"] for c in candles_15m[-6:-3])
        higher_lows = lows_recent >= lows_prev * 0.999

        closed_4_vol = sum(c["volume"] for c in box_candles) / 4
        vcp_vol_ratio = (closed_4_vol / avg_vol) if avg_vol > 0 else 1.0
        is_dry_volume = vcp_vol_ratio <= 1.10

        curr_body = abs(curr["close"] - curr["open"])
        not_dump = (closed["close"] >= closed["open"] or closed["close"] >= e21) and not (curr["close"] < curr["open"] and curr_body > 0.35 * a15)

        if all_closed_above_e50 and curr_above_e21 and is_near_ema and higher_lows and recent_box <= 0.020 and is_dry_volume and not_dump:
            patterns.append({
                "key": "vcp",
                "name": f"Сжатие VCP над EMA21/50 (бокс {recent_box*100:.1f}%, vol x{vcp_vol_ratio:.1f})",
                "mode": "sniper",
                "base_bonus": 3.0,
            })

    # =========================================================================
    # 4. CLEAN BREAKOUT (Истинный пробой + Фильтр «истощения импульса»)
    # =========================================================================
    if recent_high > 0 and a15 > 0 and e50 > 0:
        dist_from_ema50_atr = (curr["close"] - e50) / a15
        base_slice = candles_15m[-8:-2]
        pre_breakout_box = (
            (max(c["high"] for c in base_slice) - min(c["low"] for c in base_slice)) / curr["close"]
            if base_slice and curr["close"] > 0 else 0.0
        )
        not_exhausted = (dist_from_ema50_atr <= 2.8) and (pre_breakout_box <= 0.042) and (r <= 73)

        if not_exhausted:
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
                    "name": f"Истинный пробой базы ${recent_high:.4f} (vol x{b_vol:.1f})",
                    "mode": "rocket",
                    "base_bonus": 3.0,
                })

    # =========================================================================
    # 5. FAILED AUCTION (Бычье поглощение)
    # =========================================================================
    closed_engulf = (
        prev2["close"] < prev2["open"] and
        closed["close"] > closed["open"] and
        closed["close"] >= prev2["open"] and
        closed["open"] <= prev2["close"] and
        closed["volume"] >= prev2["volume"] and
        curr["close"] >= closed["open"]
    )
    live_engulf = (
        closed["close"] < closed["open"] and
        curr["close"] > curr["open"] and
        curr["close"] >= closed["open"] and
        curr["open"] <= closed["close"] and
        curr["volume"] >= closed["volume"] * 1.15
    )

    if closed_engulf or live_engulf:
        patterns.append({
            "key": "engulfing",
            "name": "Бычье поглощение (ловушка продавцов)",
            "mode": "sniper",
            "base_bonus": 2.0,
        })

    return patterns
