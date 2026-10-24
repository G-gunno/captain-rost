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
    "TOP20": 0.010,  # 1.0%
    "MID":   0.013,  # 1.3%
    "SMALL": 0.016,  # 1.6%
    "MICRO": 0.018,  # 1.8%
}

# Снижены потолки максимального стопа для защиты капитала микро/малых тиров
TIER_MAX_SL = {
    "TOP20": 2.0,
    "MID":   2.2,
    "SMALL": 2.0,
    "MICRO": 2.2,
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"runner_dist": 0.009, "lock_trigger_pct": 1.6, "lock_dist": 0.006},
    "MID":   {"runner_dist": 0.011, "lock_trigger_pct": 2.0, "lock_dist": 0.008},
    "SMALL": {"runner_dist": 0.014, "lock_trigger_pct": 2.3, "lock_dist": 0.009},
    "MICRO": {"runner_dist": 0.018, "lock_trigger_pct": 2.6, "lock_dist": 0.012},
}

TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 1.5,
        "risk_per_trade_core": 0.50,
        "risk_per_trade_alt": 0.25,
        "min_rr_sniper": 1.20,
        "min_rr_alt": 1.05,
        "require_grade_a_for_alt": True,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 2.0,
        "risk_per_trade_core": 0.75,
        "risk_per_trade_alt": 0.50,
        "min_rr_sniper": 1.15,
        "min_rr_alt": 1.00,
        "require_grade_a_for_alt": True,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 3.0,
        "risk_per_trade_core": 1.00,
        "risk_per_trade_alt": 0.75,
        "min_rr_sniper": 1.05,
        "min_rr_alt": 0.95,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 5.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 1.00,
        "min_rr_alt": 0.90,
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

    if entry_mode in ("sniper", "rocket") and pos_in_48h > 0.70:
        return "B"
    if corr > 0.55:
        return "B"

    confluence = 0
    patterns = cand.get("patterns", [])
    keys = cand.get("reason_keys", [])
    sv = cand.get("signal_values", {})
    sup_touches = cand.get("sup_touches", 0)

    strong_pats = [p for p in patterns if p in ("pinbar", "divergence", "vcp", "breakout")]
    if len(strong_pats) >= 1:
        confluence += 2
    elif len(patterns) >= 1:
        confluence += 1

    if sup_touches >= 4:
        confluence += 2
    elif sup_touches == 3:
        confluence += 1

    if cand.get("gate_1h") == "1H_TREND_OK" and ("mtf_dip" in keys or pos_in_48h <= 0.65):
        confluence += 1

    if sv.get("volume", 1.0) >= 1.45:
        confluence += 1

    if corr < 0.25:
        confluence += 1

    if not strong_pats and sup_touches < 4:
        return "B"

    req_confluence = 5 if getattr(learner, "grade_a_streak", 0) <= -2 else 4
    return "A+" if confluence >= req_confluence else "B"


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
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.016)
    max_sl_allowed = TIER_MAX_SL.get(tier, 2.0)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry and (entry - sup) <= 1.8 * a:
        struct_sl = round(sup * 0.997, 8)
        candidate_dist = max(entry - struct_sl, entry * sl_floor_pct)
        if (candidate_dist / entry) * 100 <= max_sl_allowed:
            sl_dist = candidate_dist
            sl = round(entry - sl_dist, 8)

    if sl_dist <= 0:
        base_mult = 0.85 if is_mom else (1.50 if entry_mode == "reversal" else 1.20)
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

    tp_dist_raw = max(1.65 * a * tp_tuning, sl_dist * 1.35, entry * tp_floor_pct)
    tp = round(entry + tp_dist_raw, 8)
    if min_rr is None:
        min_rr = 1.05 if entry_mode == "sniper" else 0.95

    if entry_mode == "reversal" and e21_1h and e50_1h and e21_1h < e50_1h and e21_1h > entry * 1.008:
        cap_1h = round(e21_1h * 0.998, 8)
        if cap_1h < tp and (cap_1h - entry) / sl_dist >= 0.85:
            tp = cap_1h
            min_rr = min(min_rr, 0.85)

    if res and res > entry and res_touches >= 4:
        res_dist_pct = (res - entry) / entry * 100
        if res_dist_pct < 0.35:
            return None, None, False, f"Капитальная стена 1H в упор ({res_dist_pct:.2f}% < 0.35%)"

        res_cap_tp = round(res * 0.997, 8)
        if res_cap_tp < tp:
            if (res_cap_tp - entry) / sl_dist >= 0.85:
                tp = res_cap_tp
                min_rr = min(min_rr, 0.85)
            else:
                return None, None, False, f"Капитальная стена 1H не дает забрать 0.85x RR (RR={(res_cap_tp - entry)/sl_dist:.2f})"

    rr = (tp - entry) / sl_dist
    if tp <= entry or round(rr, 2) < min_rr:
        return None, None, False, f"Недостаточный RR ({round(rr, 2)} < {min_rr})"

    return sl, tp, True, "OK"


def calc_runner_target(symbol: str, current_price: float, candles_15m: list, candles_1h: list = None, pos: dict = None) -> tuple:
    if not candles_15m or len(candles_15m) < 25:
        return round(current_price * 1.03, 8), True, "static"

    closes = [c["close"] for c in candles_15m]
    e21 = ema(closes, 21)[-1]
    e50 = ema(closes, 50)[-1]
    r15 = rsi(closes)
    a15 = atr(candles_15m)

    dist_from_e50_atr = ((current_price - e50) / a15) if a15 > 0 else 0.0
    if r15 > 75 or dist_from_e50_atr > 3.5:
        climax_tp = round(max(current_price, candles_15m[-1]["high"]) * 1.001, 8)
        return climax_tp, False, "climax_freeze"

    entry_mode = (pos or {}).get("entry_mode", "sniper")
    if candles_1h and len(candles_1h) >= 50:
        closes_1h = [c["close"] for c in candles_1h]
        e21_1h = ema(closes_1h, 21)[-1]
        e50_1h = ema(closes_1h, 50)[-1]
        if entry_mode == "reversal" and e21_1h < e50_1h and current_price < e50_1h:
            if e21_1h > current_price * 1.005:
                return round(e21_1h * 0.998, 8), False, "1h_ema21_cap"

    recent_box = (max(c["high"] for c in candles_15m[-6:]) - min(c["low"] for c in candles_15m[-6:])) / current_price if current_price > 0 else 0.0
    if recent_box <= 0.015 and e21 <= e50 * 1.001:
        old_tp = (pos or {}).get("tp", current_price * 1.02)
        return round(max(old_tp, current_price * 1.012), 8), False, "shelf_freeze"

    all_res, _ = get_all_sr_levels(candles_15m, candles_1h)
    res_above = [r[0] for r in all_res if r[0] > current_price * 1.012 and r[1] >= 4]

    mult_atr = 3.2 if (e21 > e50 and r15 >= 58) else 2.5
    corridor_ceiling = e21 + mult_atr * a15
    target = max(corridor_ceiling, current_price + 2.2 * a15, current_price * 1.025)

    if res_above:
        res_above.sort()
        wall = res_above[0] * 0.997
        if wall > current_price * 1.015:
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
    tp_target_pct = ((init_tp - avg_p) / avg_p * 100) if init_tp > avg_p else 3.0

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

    is_climax = (r15 > 75) or (a15 > 0 and e50 > 0 and (last_p - e50) > 3.5 * a15)

    ema_ratchet_sl = 0.0
    if a15 > 0 and e21 > 0 and e50 > 0:
        if is_climax and profit_pct >= 1.2:
            ema_ratchet_sl = e21 - 0.40 * a15
        elif is_rocket:
            if last_p > e21:
                ema_ratchet_sl = e21 - 0.45 * a15
        elif entry_mode == "sniper":
            if last_p > e50:
                ema_ratchet_sl = e50 - 0.80 * a15
        elif entry_mode == "reversal":
            if pos.get("tp1_done") or (last_p > e50 and e21 >= e50 * 0.998):
                ema_ratchet_sl = e50 - 0.80 * a15

        if ema_ratchet_sl >= last_p * 0.996:
            ema_ratchet_sl = last_p * 0.995

    if pos.get("tp1_done"):
        tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or 0.0)
        runner_dist = cfg["runner_dist"] * (0.80 if is_rocket else 1.0)
        runner_trail = round(max_p * (1 - runner_dist), 8)
        candidate = max(breakeven, runner_trail, ema_ratchet_sl)
        if candidate > new_sl:
            new_sl = candidate

        if tp1_ref > 0 and new_sl >= tp1_ref * 0.9995:
            new_state = "ABOVE_TP1"
        elif new_sl > breakeven * 1.003:
            new_state = "TRAILING_RUNNER"
        else:
            new_state = "BREAKEVEN"
    else:
        if is_rocket:
            if profit_pct >= max(1.0, tp_target_pct * 0.50):
                if breakeven > new_sl:
                    new_sl = breakeven
                    new_state = "BREAKEVEN"
            if a15 > 0 and e21 > 0 and last_p > e21:
                e21_sl = e21 - 0.45 * a15
                if e21_sl > new_sl:
                    new_sl = e21_sl
                    new_state = "PROFIT_LOCK" if new_sl > breakeven * 1.002 else "BREAKEVEN"

        if ema_ratchet_sl > new_sl and profit_pct >= 0.8:
            new_sl = ema_ratchet_sl
            if new_sl >= breakeven * 1.003:
                new_state = "PROFIT_LOCK"
            elif new_sl >= breakeven:
                new_state = "BREAKEVEN"

        if profit_pct >= cfg["lock_trigger_pct"] or (is_climax and profit_pct >= 1.5):
            lock_sl = round(max_p * (1 - cfg["lock_dist"]), 8)
            candidate = max(breakeven, lock_sl, ema_ratchet_sl)
            if candidate > new_sl:
                new_sl = candidate
                new_state = "PROFIT_LOCK" if new_sl > breakeven * 1.002 else "BREAKEVEN"

    new_sl = round(new_sl, 8)

    if (new_sl > current_sl + 1e-7) or (new_state != current_state and new_sl >= current_sl):
        tag_desc = sl_emoji(new_state)
        return True, new_sl, new_state, tag_desc

    return False, current_sl, current_state, ""
