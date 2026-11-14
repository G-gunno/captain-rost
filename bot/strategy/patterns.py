from bot.strategy.indicators import rsi, ema


def detect_patterns(candles_15m: list, a15: float, e50: float, e21: float, recent_high: float) -> list:
    if not candles_15m or len(candles_15m) < 25:
        return []

    patterns = []
    curr = candles_15m[-1]
    closed = candles_15m[-2]
    prev2 = candles_15m[-3]
    closes = [c["close"] for c in candles_15m]

    vols = [c["volume"] for c in candles_15m]
    avg_vol = sum(vols[-22:-2]) / 20 if len(vols) >= 22 else (sum(vols) / max(1, len(vols)))
    vol_ratio_curr = (curr["volume"] / avg_vol) if avg_vol > 0 else 1.0
    vol_ratio_closed = (closed["volume"] / avg_vol) if avg_vol > 0 else 1.0
    r = rsi(closes)

    local_drop_pct = ((recent_high - curr["close"]) / recent_high * 100) if recent_high > curr["close"] else 0.0
    waterfall_stabilized = (
        local_drop_pct <= 9.0 or
        (closed["low"] >= prev2["low"] * 0.9985 and curr["low"] >= closed["low"] * 0.999 and closed["close"] > closed["open"])
    )

    # 1. SPRING PINBAR (строго по закрытой 15m свече + подтверждение зелёным стартом следующей свечи)
    closed_range = closed["high"] - closed["low"]
    if closed_range > 0:
        closed_body_low = min(closed["open"], closed["close"])
        closed_lwick = closed_body_low - closed["low"]
        is_closed_pin = (
            (closed_lwick / closed_range) >= 0.50 and
            closed["close"] >= closed["open"] and
            closed["close"] >= closed["low"] + 0.60 * closed_range and
            vol_ratio_closed >= 1.25 and
            curr["close"] >= closed["close"] * 0.999 and
            curr["low"] >= closed_body_low * 0.999
        )
        if is_closed_pin:
            pin_wick_pct = (closed_lwick / closed_range) * 100
            wick_entry_price = round(closed_body_low, 8)
            if (r <= 45 or curr["close"] <= e50 * 1.003) and waterfall_stabilized:
                patterns.append({
                    "key": "pinbar",
                    "name": f"Пин-бар выкупа на дне (хвост {pin_wick_pct:.0f}%, vol x{vol_ratio_closed:.1f})",
                    "mode": "reversal",
                    "base_bonus": 2.8,
                    "wick_entry_price": wick_entry_price,
                })
            elif e21 >= e50 and curr["close"] >= e21 * 0.998:
                patterns.append({
                    "key": "pinbar",
                    "name": f"Молот по тренду (хвост {pin_wick_pct:.0f}%, vol x{vol_ratio_closed:.1f})",
                    "mode": "sniper",
                    "base_bonus": 2.0,
                    "wick_entry_price": wick_entry_price,
                })

    # 2. BULLISH DIVERGENCE
    rsi_now = r
    past_closes = closes[:-6]
    rsi_past = rsi(past_closes)
    low_now = min(c["low"] for c in candles_15m[-6:])
    low_past = min(c["low"] for c in candles_15m[-18:-6])
    turning_up = (closed["close"] > closed["open"] and curr["close"] >= closed["close"])

    if waterfall_stabilized and low_now <= low_past * 1.003 and rsi_past <= 36.0 and rsi_now > (rsi_past + 4.0) and rsi_now <= 48 and turning_up:
        w_base_entry = round(min(curr["close"] * 0.998, max(low_now * 1.003, closed["open"])), 8)
        patterns.append({
            "key": "divergence",
            "name": f"Бычья дивергенция RSI ({rsi_past:.0f} -> {rsi_now:.0f})",
            "mode": "reversal",
            "base_bonus": 2.8,
            "wick_entry_price": w_base_entry,
        })

    # 3. VCP COMPRESSION (только над растущими EMA21/50 без признаков разворота вниз)
    e21_series = ema(closes, 21)
    e21_rising = len(e21_series) >= 5 and e21_series[-1] > e21_series[-4] * 1.0004

    if e50 > 0 and e21 > (e50 * 1.001) and e21_rising:
        box_candles = candles_15m[-5:-1]
        all_closed_above_e21 = all(c["close"] >= e21 * 0.999 for c in box_candles)
        curr_above_e21 = curr["close"] >= e21 and curr["close"] >= curr["open"]
        is_near_ema = 1.000 <= (curr["close"] / e50) <= 1.015
        recent_box = (max(c["high"] for c in candles_15m[-5:]) - min(c["low"] for c in candles_15m[-5:])) / curr["close"]
        lows_recent = min(c["low"] for c in candles_15m[-3:])
        lows_prev = min(c["low"] for c in candles_15m[-6:-3])
        higher_lows = lows_recent >= lows_prev * 1.000
        closed_4_vol = sum(c["volume"] for c in box_candles) / 4
        vcp_vol_ratio = (closed_4_vol / avg_vol) if avg_vol > 0 else 1.0
        is_dry_volume = vcp_vol_ratio <= 1.05

        if all_closed_above_e21 and curr_above_e21 and is_near_ema and higher_lows and recent_box <= 0.018 and is_dry_volume and closed["close"] >= closed["open"]:
            patterns.append({
                "key": "vcp",
                "name": f"Сжатие VCP над EMA21 (бокс {recent_box*100:.1f}%, vol x{vcp_vol_ratio:.1f})",
                "mode": "sniper",
                "base_bonus": 2.6,
            })

    # 4. CLEAN BREAKOUT
    if recent_high > 0 and a15 > 0 and e50 > 0:
        dist_from_ema50_atr = (curr["close"] - e50) / a15
        base_slice = candles_15m[-8:-2]
        pre_breakout_box = (
            (max(c["high"] for c in base_slice) - min(c["low"] for c in base_slice)) / curr["close"]
            if base_slice and curr["close"] > 0 else 0.0
        )
        not_exhausted = (dist_from_ema50_atr <= 2.5) and (pre_breakout_box <= 0.040) and (r <= 71)

        if not_exhausted:
            closed_green = closed["close"] > closed["open"]
            closed_body = abs(closed["close"] - closed["open"])
            closed_uwick = closed["high"] - max(closed["open"], closed["close"])
            closed_solid = (closed_body / closed_range) >= 0.58 if closed_range > 0 else False
            closed_clean = (closed_uwick / closed_range) <= 0.20 if closed_range > 0 else False

            brk_confirmed_closed = (
                closed["close"] > recent_high and closed_green and
                closed_solid and closed_clean and vol_ratio_closed >= 1.35 and
                curr["close"] >= closed["close"] * 0.999
            )

            curr_range = curr["high"] - curr["low"]
            curr_green = curr["close"] > curr["open"]
            curr_body = abs(curr["close"] - curr["open"])
            curr_uwick = curr["high"] - max(curr["open"], curr["close"])
            curr_solid = (curr_body / curr_range) >= 0.62 if curr_range > 0 else False
            curr_clean = (curr_uwick / curr_range) <= 0.18 if curr_range > 0 else False

            brk_live = (
                curr["close"] > recent_high * 1.001 and curr_green and
                curr_solid and curr_clean and vol_ratio_curr >= 1.55
            )

            if brk_confirmed_closed or brk_live:
                b_vol = vol_ratio_curr if brk_live else vol_ratio_closed
                patterns.append({
                    "key": "breakout",
                    "name": f"Истинный пробой базы ${recent_high:.4f} (vol x{b_vol:.1f})",
                    "mode": "rocket",
                    "base_bonus": 3.0,
                })

    # 5. БЫЧЬЕ ПОГЛОЩЕНИЕ НА РАЗВОРОТЕ ПРОСАДКИ (строго по закрытой 15m свече!)
    closed_engulf = (
        prev2["close"] < prev2["open"] and
        closed["close"] > closed["open"] and
        closed["close"] > prev2["high"] and
        (closed["close"] - closed["open"]) >= (prev2["open"] - prev2["close"]) * 1.10 and
        closed["volume"] >= prev2["volume"] * 1.15 and
        curr["close"] >= closed["close"] * 0.999 and
        curr["close"] >= curr["open"] * 0.9995 and
        r <= 62
    )
    if closed_engulf:
        patterns.append({
            "key": "engulfing",
            "name": "Бычье поглощение (разворот волны)",
            "mode": "sniper",
            "base_bonus": 2.2,
        })

    return patterns
