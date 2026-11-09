import time
from bot.strategy.indicators import atr, ema, rsi, get_all_sr_levels
from bot.strategy.shadow import shadow
from bot.strategy.learner import learner
from bot.utils.format import sl_emoji

TIER_SL_FLOOR = {
    "TOP20": 0.009,  # 0.9%
    "MID":   0.012,  # 1.2%
    "SMALL": 0.015,  # 1.5%
    "MICRO": 0.018,  # 1.8%
}

TIER_TP_FLOOR = {
    "TOP20": 0.012,  # 1.2%
    "MID":   0.014,  # 1.4%
    "SMALL": 0.015,  # 1.5%
    "MICRO": 0.017,  # 1.7%
}

TIER_MAX_SL = {
    "TOP20": 2.0,
    "MID":   2.2,
    "SMALL": 2.2,
    "MICRO": 2.4,
}

# Расширенная дистанция трейлинга раннера (1.5% - 2.4%), чтобы не выбивало на ретестах после TP1
TIER_TRAIL_CONFIG = {
    "TOP20": {"runner_dist": 0.015, "lock_trigger_pct": 1.85, "lock_dist": 0.006},
    "MID":   {"runner_dist": 0.018, "lock_trigger_pct": 2.05, "lock_dist": 0.007},
    "SMALL": {"runner_dist": 0.021, "lock_trigger_pct": 2.25, "lock_dist": 0.008},
    "MICRO": {"runner_dist": 0.024, "lock_trigger_pct": 2.50, "lock_dist": 0.009},
}

TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 8.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 0.98,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 10.0,
        "risk_per_trade_core": 1.50,
        "risk_per_trade_alt": 1.20,
        "min_rr_sniper": 0.96,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 12.0,
        "risk_per_trade_core": 1.75,
        "risk_per_trade_alt": 1.45,
        "min_rr_sniper": 0.95,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 16.0,
        "risk_per_trade_core": 2.20,
        "risk_per_trade_alt": 1.80,
        "min_rr_sniper": 0.92,
        "min_rr_alt": 0.82,
        "require_grade_a_for_alt": False,
    },
}


def get_trade_policy(risk_mode: str = "NORMAL") -> dict:
    return TRADE_POLICIES.get(risk_mode, TRADE_POLICIES["NORMAL"])


def calc_breakeven_price(avg_price: float, fee_pct: float = 0.10) -> float:
    if avg_price <= 0:
        return 0.0
    f = fee_pct / 100.0
    be = avg_price * (1.0 + f) / (1.0 - f) * 1.0005
    return round(be, 8)


def evaluate_setup_grade(cand: dict) -> str:
    pos_in_48h = cand.get("pos_in_48h", 0.5)
    corr = abs(cand.get("corr", 1.0))
    entry_mode = cand.get("entry_mode", "sniper")
    tier = cand.get("tier", "MICRO")
    sector = cand.get("sector", "Other")
    gate_1h = cand.get("gate_1h", "")

    if entry_mode == "sniper" and pos_in_48h > 0.62:
        return "B"
    if entry_mode == "reversal" and pos_in_48h > 0.42:
        return "B"
    if corr > 0.72 and entry_mode != "reversal":
        return "B"
    if sector == "Other" and tier == "MICRO":
        return "B"

    patterns = cand.get("patterns", [])
    strong_pats = [p for p in patterns if p in ("pinbar", "divergence", "vcp", "breakout")]
    sv = cand.get("signal_values", {})
    vol_ratio = sv.get("volume", 1.0)
    r_val = sv.get("rsi", 50.0)
    sup_touches = cand.get("sup_touches", 0)
    keys = cand.get("reason_keys", [])

    has_strong_slab_setup = (sup_touches >= 3 and (vol_ratio >= 1.15 or r_val <= 44.0))
    if not strong_pats and not has_strong_slab_setup:
        return "B"

    confluence = 2 if (strong_pats or sup_touches >= 3) else 1
    if strong_pats and sup_touches >= 3:
        confluence += 1
    if sup_touches >= 4:
        confluence += 1

    if gate_1h == "1H_TREND_OK" and ("mtf_dip" in keys or pos_in_48h <= 0.58):
        confluence += 1
    elif gate_1h == "1H_SUPPORT_ONLY" and pos_in_48h <= 0.40 and sup_touches >= 3:
        confluence += 1

    if vol_ratio >= 1.30:
        confluence += 1

    if tier in ("TOP20", "MID") or corr < 0.35:
        confluence += 1

    return "A+" if confluence >= 4 else "B"


def calc_total_open_risk_pct(positions: dict, orders: list, equity: float) -> float:
    if equity <= 0:
        return 0.0
    total_risk_usdt = 0.0

    for pos in positions.values():
        avg = pos.get("avg", 0.0)
        sl = pos.get("sl", 0.0)
        qty = pos.get("qty", 0.0)
        if avg > sl > 0 and qty > 0:
            total_risk_usdt += (avg - sl) * qty

    for o in orders:
        price = o.get("price", 0.0)
        sl = o.get("sl", 0.0)
        qty = o.get("qty", 0.0)
        if price > sl > 0 and qty > 0:
            total_risk_usdt += (price - sl) * qty

    return round((total_risk_usdt / equity) * 100.0, 3)


def calc_initial_sl_tp(symbol: str, entry: float, tier: str, a: float,
                        sup: float, sup_touches: int, res: float, res_touches: int,
                        is_mom: bool = False, entry_mode: str = "sniper",
                        min_rr: float = None, recent_low_6: float = None,
                        ema50: float = None, e21_1h: float = None, e50_1h: float = None):
    tier = tier or "SMALL"
    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.015)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.015)
    max_sl_allowed = TIER_MAX_SL.get(tier, 2.2)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    base_mult = 0.85 if is_mom else (1.25 if entry_mode == "reversal" else 1.20)
    raw_atr_sl_pct = (base_mult * a * sl_tuning / entry) * 100.0 if entry > 0 else 0.0

    if raw_atr_sl_pct > max_sl_allowed * 1.25:
        return None, None, False, f"Аномальная волатильность ATR (SL {raw_atr_sl_pct:.2f}% > {max_sl_allowed}%)"

    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry and (entry - sup) <= 1.8 * a:
        struct_sl = round(sup * 0.997, 8)
        candidate_dist = max(entry - struct_sl, entry * sl_floor_pct)
        if (candidate_dist / entry) * 100 <= max_sl_allowed:
            sl_dist = candidate_dist
            sl = round(entry - sl_dist, 8)

    if sl_dist <= 0:
        sl_dist_raw = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
        sl_dist = min(sl_dist_raw, entry * (max_sl_allowed / 100.0))
        sl = round(entry - sl_dist, 8)

    if recent_low_6 and recent_low_6 > 0 and recent_low_6 < entry:
        wick_safe_sl = round(recent_low_6 * 0.997, 8)
        if wick_safe_sl < sl:
            wick_dist_pct = (entry - wick_safe_sl) / entry * 100
            if wick_dist_pct <= max_sl_allowed:
                sl = wick_safe_sl
                sl_dist = entry - sl

    if ema50 and ema50 > 0 and entry > ema50:
        ema50_safe_sl = round(ema50 - 0.6 * a, 8)
        if sl > ema50_safe_sl and abs(sl - ema50) <= 0.6 * a:
            ema_dist_pct = (entry - ema50_safe_sl) / entry * 100
            if ema_dist_pct <= max_sl_allowed:
                sl = ema50_safe_sl
                sl_dist = entry - sl

    if sl >= entry or (sl_dist / entry * 100) > max_sl_allowed:
        return None, None, False, "Недопустимая дистанция SL"

    tp_atr_mult = 1.28 if entry_mode == "reversal" else (1.50 if is_mom else 1.32)
    rr_target = 1.06 if entry_mode == "reversal" else 1.12
    tp_dist_raw = max(tp_atr_mult * a * tp_tuning, sl_dist * rr_target, entry * tp_floor_pct)
    if not is_mom:
        tp_dist_raw = min(tp_dist_raw, entry * 0.0195)
    tp = round(entry + tp_dist_raw, 8)
    if min_rr is None:
        min_rr = 0.95 if entry_mode == "sniper" else 0.84

    # Для reversal в часовом даунтренде (e21_1h < e50_1h) ограничиваем TP1 под падающей 1H EMA21 (фикс STRKUSDT)
    if entry_mode == "reversal" and e21_1h and e50_1h and e21_1h < e50_1h:
        if e21_1h > entry * 1.0095:
            cap_e21 = round(e21_1h * 0.9985, 8)
            if cap_e21 < tp:
                tp = cap_e21
                min_rr = min(min_rr, (tp - entry) / sl_dist)
        elif e50_1h > entry * 1.010:
            cap_e50 = round(e50_1h * 0.998, 8)
            if cap_e50 < tp:
                tp = cap_e50
                min_rr = min(min_rr, (tp - entry) / sl_dist)

    # Проверяем только настоящие капитальные стены (вес >= 6, то есть минимум 3 часовых пика)
    if res and res > entry and res_touches >= 6:
        res_dist_pct = (res - entry) / entry * 100
        min_wall_dist = 0.18 if is_mom else 0.24
        if res_dist_pct < min_wall_dist:
            return None, None, False, f"Капитальная стена 1H в упор ({res_dist_pct:.2f}% < {min_wall_dist:.2f}%)"

        res_cap_tp = round(res * 0.998, 8)
        if res_cap_tp < tp:
            wall_rr = (res_cap_tp - entry) / sl_dist
            wall_gain_pct = (res_cap_tp - entry) / entry * 100
            if wall_rr >= 0.68 or wall_gain_pct >= 0.82:
                tp = res_cap_tp
                min_rr = min(min_rr, wall_rr)
            else:
                return None, None, False, f"Капитальная стена 1H слишком близко для TP1 ({wall_gain_pct:.2f}%)"

    rr = (tp - entry) / sl_dist
    if ( rr + 0.015 ) < min_rr or tp <= entry:
        return None, None, False, f"Недостаточный RR ({rr:.2f} < {min_rr:.2f})"

    return sl, tp, True, "OK"


def calc_runner_target(symbol: str, current_price: float, candles_15m: list, candles_1h: list = None, pos: dict = None) -> tuple:
    if not candles_15m or len(candles_15m) < 25:
        return round(current_price * 1.045, 8), True, "static"

    closes = [c["close"] for c in candles_15m]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    r15 = rsi(closes)
    a15 = atr(candles_15m)

    dist_from_e50_atr = ((current_price - e50) / a15) if a15 > 0 else 0.0
    if r15 > 78 or dist_from_e50_atr > 4.5:
        climax_tp = round(max(current_price, candles_15m[-1]["high"]) * 1.022, 8)
        return climax_tp, False, "climax_freeze"

    recent_box = (max(c["high"] for c in candles_15m[-6:]) - min(c["low"] for c in candles_15m[-6:])) / current_price if current_price > 0 else 0.0
    if recent_box <= 0.012 and e21 <= e50 * 0.999:
        old_tp = (pos or {}).get("tp", current_price * 1.03)
        return round(max(old_tp, current_price * 1.020), 8), False, "shelf_freeze"

    all_res, _ = get_all_sr_levels(candles_15m, candles_1h)
    res_above = [r[0] for r in all_res if r[0] > current_price * 1.020 and r[1] >= 6]

    mult_atr = 4.2 if (e21 > e50 and r15 >= 56) else 3.2
    corridor_ceiling = e21 + mult_atr * a15
    target = max(corridor_ceiling, current_price + 3.0 * a15, current_price * 1.038)

    if res_above:
        res_above.sort()
        wall = res_above[0] * 0.997
        if wall > current_price * 1.022:
            target = min(target, wall)

    return round(target, 8), True, "trend_expand"


def update_position_sl(pos: dict, max_p: float, last_price: float = None, candles_15m: list = None) -> tuple:
    avg_p = pos.get("avg", 0.0)
    if avg_p <= 0:
        return False, pos.get("sl"), pos.get("sl_state", "SHIELD"), ""

    last_p = last_price or max_p
    tier = pos.get("tier") or "SMALL"
    entry_mode = pos.get("entry_mode", "sniper")
    is_rocket = (entry_mode == "rocket" or pos.get("is_momentum", False))
    cfg = TIER_TRAIL_CONFIG.get(tier, TIER_TRAIL_CONFIG["SMALL"])

    current_sl = round(float(pos.get("sl", 0.0)), 8)
    new_sl = current_sl
    current_state = pos.get("sl_state", "SHIELD")
    new_state = current_state

    breakeven = calc_breakeven_price(avg_p)
    profit_pct = (max_p - avg_p) / avg_p * 100
    tp_price = float(pos.get("tp", 0.0))
    init_tp = float(pos.get("initial_tp") or tp_price)
    tp_target_pct = ((init_tp - avg_p) / avg_p * 100) if init_tp > avg_p else 1.8

    if candles_15m and len(candles_15m) >= 50:
        closes = [c["close"] for c in candles_15m]
        pos["_ema21"] = ema(closes, 21)[-1]
        pos["_ema50"] = ema(closes, 50)[-1]
        pos["_atr15"] = atr(candles_15m)
        pos["_rsi15"] = rsi(closes)

    e21 = pos.get("_ema21", 0.0)
    e50 = pos.get("_ema50", 0.0)
    a15 = pos.get("_atr15", 0.0)
    r15 = pos.get("_rsi15", 50.0)

    is_climax = (r15 > 78) or (a15 > 0 and e50 > 0 and (last_p - e50) > 4.2 * a15)

    ema_ratchet_sl = 0.0
    if a15 > 0 and e21 > 0 and e50 > 0:
        if is_climax and profit_pct >= 1.8:
            ema_ratchet_sl = e21 - 0.55 * a15
        elif pos.get("tp1_done"):
            # После TP1 даём раннеру широкий структурный коридор под EMA50 - 0.65*ATR (или EMA21 - 0.85*ATR для ракет)
            if is_rocket and last_p > e21 and profit_pct >= 2.6:
                ema_ratchet_sl = e21 - 0.85 * a15
            elif last_p > e50 and profit_pct >= 2.2:
                ema_ratchet_sl = e50 - 0.65 * a15

        if ema_ratchet_sl >= last_p * 0.992:
            ema_ratchet_sl = last_p * 0.991

    if pos.get("tp1_done"):
        tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or 0.0)
        runner_dist = cfg["runner_dist"] * (0.90 if is_rocket else 1.0)
        runner_trail = round(max_p * (1 - runner_dist), 8)

        # Базовый пол раннера сразу после взятия TP1: +0.30% от входа (оставляем ~1.2% запаса хода на ретест пробитого уровня!)
        runner_floor = round(max(breakeven, avg_p * 1.0030), 8)

        # Переносим стоп на уровень TP1 (🛡🔥) только когда цена реально улетела выше TP1 минимум на +1.2% (или +1.2 ATR)
        if tp1_ref > 0 and ((a15 > 0 and max_p >= tp1_ref + 1.20 * a15) or max_p >= tp1_ref * 1.012):
            runner_floor = max(runner_floor, round(tp1_ref * 1.0005, 8))

        candidate = max(breakeven, runner_floor, runner_trail, ema_ratchet_sl)
        if candidate > new_sl and last_p > candidate * 1.002:
            new_sl = candidate

        if tp1_ref > 0 and new_sl >= tp1_ref * 0.9995:
            new_state = "ABOVE_TP1"
        elif new_sl > breakeven * 1.0035:
            new_state = "TRAILING_RUNNER"
        else:
            new_state = "BREAKEVEN"
    else:
        # 1. Анти-зомби защита: если сделка висит >= 90 минут и вышла в плюс >= +0.38% -> переводим в БУ (🛡🔒)
        pos_age = int(time.time()) - int(pos.get("entry_time", int(time.time())))
        if pos_age >= 5400 and profit_pct >= 0.38 and breakeven > new_sl and last_p > breakeven * 1.0015:
            new_sl = breakeven
            new_state = "BREAKEVEN"

        # 2. Стандартный перевод в БУ (🛡🔒) при прохождении 70% пути к TP1 (но не ранее +1.15%)
        be_trigger = max(1.15, tp_target_pct * 0.70)
        if profit_pct >= be_trigger and breakeven > new_sl and last_p > breakeven * 1.002:
            new_sl = breakeven
            new_state = "BREAKEVEN"

        # 3. До взятия TP1 НЕ зажимаем сделку узким PROFIT_LOCK (фикс QNT #5), кроме экстремальной свечи кульминации
        if is_climax and profit_pct >= cfg["lock_trigger_pct"]:
            lock_sl = round(max_p * (1 - cfg["lock_dist"]), 8)
            if lock_sl > breakeven and lock_sl > new_sl and last_p > lock_sl * 1.002:
                new_sl = lock_sl
                new_state = "PROFIT_LOCK"

    new_sl = round(new_sl, 8)

    if (new_sl > current_sl + 1e-7) or (new_state != current_state and new_sl >= current_sl):
        tag_desc = sl_emoji(new_state)
        return True, new_sl, new_state, tag_desc

    return False, current_sl, current_state, ""
