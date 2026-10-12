from bot.strategy.indicators import atr, find_sr_levels, get_all_sr_levels
from bot.strategy.shadow import shadow

TIER_SL_FLOOR = {
    "TOP20": 0.010,  # 1.0%
    "MID":   0.015,  # 1.5%
    "SMALL": 0.020,  # 2.0%
    "MICRO": 0.025,  # 2.5%
}

TIER_TP_FLOOR = {
    "TOP20": 0.012,  # 1.2%
    "MID":   0.020,  # 2.0%
    "SMALL": 0.025,  # 2.5%
    "MICRO": 0.028,  # 2.8%
}

TIER_MAX_SL = {
    "TOP20": 2.0,   # Макс стоп 2.0%
    "MID":   2.8,   # Макс стоп 2.8%
    "SMALL": 3.5,   # Макс стоп 3.5%
    "MICRO": 4.5,   # Макс стоп 4.5%
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"be_trigger": 0.007, "runner_dist": 0.008, "pre_tp_trigger": 1.5, "pre_tp_dist": 0.006},
    "MID":   {"be_trigger": 0.010, "runner_dist": 0.013, "pre_tp_trigger": 2.0, "pre_tp_dist": 0.010},
    "SMALL": {"be_trigger": 0.012, "runner_dist": 0.016, "pre_tp_trigger": 2.2, "pre_tp_dist": 0.012},
    "MICRO": {"be_trigger": 0.014, "runner_dist": 0.022, "pre_tp_trigger": 2.5, "pre_tp_dist": 0.018},
}


def calc_initial_sl_tp(symbol: str, entry: float, tier: str, a: float, 
                        sup: float, sup_touches: int, res: float, res_touches: int,
                        is_mom: bool = False, entry_mode: str = "sniper"):
    tier = tier or "SMALL"
    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.020)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.028)
    max_sl_allowed = TIER_MAX_SL.get(tier, 3.5)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    # 1. Расчет защитного стоп-лосса
    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry:
        struct_sl = round(sup * 0.997, 8)
        candidate_dist = entry - struct_sl
        candidate_dist_pct = (candidate_dist / entry) * 100
        if candidate_dist_pct <= max_sl_allowed:
            sl = struct_sl
            sl_dist = candidate_dist

    if sl_dist <= 0:
        base_mult = 0.75 if is_mom else (2.0 if entry_mode == "reversal" else 1.2)
        sl_dist_raw = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
        sl_dist = min(sl_dist_raw, entry * (max_sl_allowed / 100.0))
        sl = round(entry - sl_dist, 8)

    if sl >= entry or (sl_dist / entry * 100) > max_sl_allowed:
        return None, None, False, "Недопустимая дистанция SL"

    # 2. Расчет первичного тейк-профита (TP1)
    tp_dist_raw = max(2.0 * a * tp_tuning, sl_dist * 1.5, entry * tp_floor_pct)
    tp = round(entry + tp_dist_raw, 8)
    min_rr = 1.35 if entry_mode == "sniper" else 1.1

    if res and res > entry:
        res_dist_pct = (res - entry) / entry * 100
        if res_dist_pct < 0.8:
            return None, None, False, f"Стена сопротивления в упор ({res_dist_pct:.2f}% < 0.8%)"

        res_cap_tp = round(res * 0.997, 8)
        if res_cap_tp < tp:
            if (res_cap_tp - entry) / sl_dist >= 1.0:
                tp = res_cap_tp
                min_rr = 1.0
            else:
                return None, None, False, "Стена не дает забрать даже 1.0x RR"

    rr = (tp - entry) / sl_dist
    if tp <= entry or round(rr, 2) < min_rr:
        return None, None, False, f"Недостаточный RR ({round(rr, 2)} < {min_rr})"

    return sl, tp, True, "OK"


def calc_runner_target(symbol: str, current_price: float, candles_15m: list, candles_1h: list = None) -> float:
    all_res, _ = get_all_sr_levels(candles_15m, candles_1h)
    
    res_above = [r[0] for r in all_res if r[0] > current_price * 1.010]
    if res_above:
        res_above.sort()
        return round(res_above[0] * 0.997, 8)

    a = atr(candles_15m)
    return round(current_price + max(2.5 * a, current_price * 0.035), 8)


def update_position_sl(pos: dict, max_p: float) -> tuple:
    """
    Машина состояний стоп-лосса:
    SHIELD -> BREAKEVEN -> PROFIT_LOCK -> TRAILING_RUNNER
    Полностью защищена от float-дребезга (IEEE 754) и повторных ложных срабатываний.
    """
    avg_p = pos.get("avg", 0.0)
    if avg_p <= 0:
        return False, pos.get("sl"), pos.get("sl_state", "SHIELD"), ""

    tier = pos.get("tier") or "SMALL"
    cfg = TIER_TRAIL_CONFIG.get(tier, TIER_TRAIL_CONFIG["SMALL"])

    current_sl = round(float(pos.get("sl", 0.0)), 8)
    new_sl = current_sl
    current_state = pos.get("sl_state", "SHIELD")
    new_state = current_state

    breakeven = round(avg_p * 1.0025, 8)
    profit_pct = (max_p - avg_p) / avg_p * 100

    # 1. Трейлинг раннера (после взятия TP1)
    if pos.get("tp1_done"):
        new_state = "TRAILING_RUNNER"
        runner_trail = round(max_p * (1 - cfg["runner_dist"]), 8)
        min_runner_sl = round(avg_p * 1.005, 8)
        new_sl = max(new_sl, runner_trail, min_runner_sl)
    else:
        # 2. До TP1: фиксация гарантированного профита
        if profit_pct >= cfg["pre_tp_trigger"]:
            new_state = "PROFIT_LOCK"
            new_sl = max(new_sl, round(max_p * (1 - cfg["pre_tp_dist"]), 8))
        # 3. До TP1: ранний перевод в безубыток
        elif max_p >= avg_p * (1 + cfg["be_trigger"]):
            if new_sl < breakeven:
                new_state = "BREAKEVEN"
                new_sl = breakeven

    new_sl = round(new_sl, 8)

    # Защита от бесконечного цикла: фиксируем изменение ТОЛЬКО при реальном росте цены
    if (new_sl > current_sl + 1e-7) or (new_state != current_state and new_sl >= current_sl):
        tag_map = {
            "SHIELD": "🛡 Защита",
            "BREAKEVEN": "🔒 БУ",
            "PROFIT_LOCK": "💼 Профит-лок",
            "TRAILING_RUNNER": f"📈 Трейлинг ({tier})"
        }
        tag_desc = tag_map.get(new_state, "Трейлинг")
        return True, new_sl, new_state, tag_desc

    return False, current_sl, current_state, ""
