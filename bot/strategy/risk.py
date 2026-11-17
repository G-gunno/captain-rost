import time
from bot.strategy.indicators import atr, ema, rsi, get_all_sr_levels
from bot.strategy.shadow import shadow
from bot.strategy.learner import learner
from bot.utils.format import sl_emoji

TIER_SL_FLOOR = {
    "TOP20": 0.009,
    "MID":   0.012,
    "SMALL": 0.015,
    "MICRO": 0.018,
}

TIER_TP_FLOOR = {
    "TOP20": 0.011,
    "MID":   0.013,
    "SMALL": 0.015,
    "MICRO": 0.017,
}

TIER_MAX_SL = {
    "TOP20": 2.0,
    "MID":   2.2,
    "SMALL": 2.4,
    "MICRO": 2.5,
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"runner_dist": 0.013, "lock_trigger_pct": 1.85, "lock_dist": 0.005},
    "MID":   {"runner_dist": 0.015, "lock_trigger_pct": 2.10, "lock_dist": 0.006},
    "SMALL": {"runner_dist": 0.018, "lock_trigger_pct": 2.35, "lock_dist": 0.007},
    "MICRO": {"runner_dist": 0.020, "lock_trigger_pct": 2.60, "lock_dist": 0.008},
}

TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 8.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 0.88,
        "min_rr_alt": 0.80,
        "require_grade_a_for_alt": False,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 10.0,
        "risk_per_trade_core": 1.50,
        "risk_per_trade_alt": 1.20,
        "min_rr_sniper": 0.88,
        "min_rr_alt": 0.80,
        "require_grade_a_for_alt": False,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 12.0,
        "risk_per_trade_core": 1.75,
        "risk_per_trade_alt": 1.45,
        "min_rr_sniper": 0.86,
        "min_rr_alt": 0.78,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 16.0,
        "risk_per_trade_core": 2.10,
        "risk_per_trade_alt": 1.75,
        "min_rr_sniper": 0.85,
        "min_rr_alt": 0.78,
        "require_grade_a_for_alt": False,
    },
}


def _round_px(val: float) -> float:
    v = float(val or 0.0)
    if v <= 0:
        return 0.0
    if v < 0.0001:
        return round(v, 10)
    return round(v, 8)


def get_trade_policy(risk_mode: str = "NORMAL") -> dict:
    return TRADE_POLICIES.get(risk_mode, TRADE_POLICIES["NORMAL"])


def calc_breakeven_price(avg_price: float, fee_pct: float = 0.10) -> float:
    if avg_price <= 0:
        return 0.0
    f = fee_pct / 100.0
    be = avg_price * (1.0 + f) / (1.0 - f) * 1.0005
    return _round_px(be)


def evaluate_setup_grade(cand: dict) -> str:
    pos_in_48h = cand.get("pos_in_48h", 0.5)
    corr = abs(cand.get("corr", 1.0))
    entry_mode = cand.get("entry_mode", "sniper")
    tier = cand.get("tier", "MICRO")
    sector = cand.get("sector", "Other")
    gate_1h = cand.get("gate_1h", "")
    sym = cand.get("symbol", "")

    if entry_mode == "reversal" and pos_in_48h > 0.35:
        return "B"
    if sector == "Other" and tier == "MICRO":
        return "B"
    if sym and learner.coin_bias(sym) < -0.35:
        return "B"

    patterns = cand.get("patterns", [])
    strong_pats = [p for p in patterns if p in ("pinbar", "divergence", "vcp", "breakout", "engulfing")]
    sv = cand.get("signal_values", {})
    vol_ratio = sv.get("volume", 1.0)
    r_val = sv.get("rsi", 50.0)
    sup_touches = cand.get("sup_touches", 0)

    is_prime_1h = gate_1h in ("1H_DIP_BOUNCE", "1H_EMA21_RECLAIM", "1H_SUPER_TREND")
    has_strong_slab_setup = (sup_touches >= 2 and pos_in_48h <= 0.40 and (vol_ratio >= 1.20 or r_val <= 45.0))

    if not strong_pats and not is_prime_1h and not has_strong_slab_setup:
        return "B"

    confluence = 2 if (strong_pats or is_prime_1h) else 1
    if strong_pats and sup_touches >= 2:
        confluence += 1
    if is_prime_1h or (gate_1h == "1H_TREND_OK" and pos_in_48h <= 0.65):
        confluence += 1
    if vol_ratio >= 1.25 or r_val <= 40:
        confluence += 1
    if tier in ("TOP20", "MID") or corr < 0.45:
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
    dna = shadow.get_coin_dna(symbol)
    wave_mult = dna.get("wave_mult", 1.0)

    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.015)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.015)
    base_max_sl = TIER_MAX_SL.get(tier, 2.4)
    max_sl_allowed = round(base_max_sl * (1.12 if wave_mult > 1.1 else 1.0), 2)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    base_mult = 0.90 if is_mom else (1.25 if entry_mode == "reversal" else 1.18)
    raw_atr_sl_pct = (base_mult * a * sl_tuning / entry) * 100.0 if entry > 0 else 0.0

    if raw_atr_sl_pct > max_sl_allowed * 1.35:
        return None, None, False, f"Аномальная волатильность ATR (SL {raw_atr_sl_pct:.2f}% > {max_sl_allowed}%)"

    # Если цена находится ПОД 1H EMA21 в даунтренде (но НЕ на ретесте сверху 1H_EMA21_RECLAIM, где dist <= 0.45%),
    # блокируем покупку в потолок (зазор 0.45% .. 1.15%)
    if e21_1h and e50_1h and e21_1h < e50_1h and e21_1h > entry * 1.0045:
        dist_to_e21_1h_pct = (e21_1h - entry) / entry * 100.0
        if dist_to_e21_1h_pct < 1.15:
            return None, None, False, f"В упор снизу к 1H EMA21 ({dist_to_e21_1h_pct:.2f}% < 1.15%)"

    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry and (entry - sup) <= 1.8 * a:
        struct_sl = _round_px(sup * 0.9960)
        candidate_dist = max(entry - struct_sl, entry * sl_floor_pct)
        if (candidate_dist / entry) * 100 <= max_sl_allowed:
            sl_dist = candidate_dist
            sl = _round_px(entry - sl_dist)

    if sl_dist <= 0:
        sl_dist_raw = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
        sl_dist = min(sl_dist_raw, entry * (max_sl_allowed / 100.0))
        sl = _round_px(entry - sl_dist)

    if recent_low_6 and recent_low_6 > 0 and recent_low_6 < entry:
        wick_safe_sl = _round_px(recent_low_6 * 0.9960)
        if wick_safe_sl < sl:
            wick_dist_pct = (entry - wick_safe_sl) / entry * 100
            if wick_dist_pct <= max_sl_allowed:
                sl = wick_safe_sl
                sl_dist = entry - sl

    if sl >= entry or (sl_dist / entry * 100) > max_sl_allowed:
        return None, None, False, "Недопустимая дистанция SL"

    if min_rr is None:
        min_rr = 0.86 if entry_mode == "sniper" else 0.78

    tp_atr_mult = 1.30 if entry_mode == "reversal" else (1.55 if is_mom else 1.35)
    rr_target = 1.00 if entry_mode == "reversal" else max(1.04, min_rr + 0.05)
    tp_dist_raw = max(tp_atr_mult * a * tp_tuning, sl_dist * rr_target, entry * tp_floor_pct)

    if not is_mom:
        max_tp1_cap = max(entry * (0.024 * wave_mult), sl_dist * max(1.05, min_rr + 0.05))
        tp_dist_raw = min(tp_dist_raw, max_tp1_cap)

    tp = _round_px(entry + tp_dist_raw)

    # При откупе со дна под 1H EMA21 ставим TP1 перед 1H EMA21, но не ниже допустимого RR (0.76), чтобы не отклонять валидные сделки!
    if e21_1h and e50_1h and e21_1h < e50_1h and e21_1h > entry * 1.0125:
        cap_e21 = _round_px(e21_1h * 0.9975)
        if cap_e21 < tp:
            wall_rr_e21 = (cap_e21 - entry) / sl_dist if sl_dist > 0 else 0.0
            if wall_rr_e21 >= 0.68:
                tp = cap_e21
                min_rr = min(min_rr, wall_rr_e21)

    if res and res > entry and res_touches >= 6:
        res_dist_pct = (res - entry) / entry * 100
        min_wall_dist = 0.18 if is_mom else 0.24
        if res_dist_pct < min_wall_dist:
            return None, None, False, f"Капитальная стена 1H в упор ({res_dist_pct:.2f}% < {min_wall_dist:.2f}%)"

        res_cap_tp = _round_px(res * 0.998)
        if res_cap_tp < tp:
            wall_rr = (res_cap_tp - entry) / sl_dist
            wall_gain_pct = (res_cap_tp - entry) / entry * 100
            if wall_rr >= 0.65 or wall_gain_pct >= 0.80:
                tp = res_cap_tp
                min_rr = min(min_rr, wall_rr)
            else:
                return None, None, False, f"Капитальная стена 1H слишком близко для TP1 ({wall_gain_pct:.2f}%)"

    rr = (tp - entry) / sl_dist if sl_dist > 0 else 0.0
    if (rr + 0.015) < min_rr or tp <= entry:
        return None, None, False, f"Недостаточный RR ({rr:.2f} < {min_rr:.2f})"

    return sl, tp, True, "OK"


def calc_runner_target(symbol: str, current_price: float, candles_15m: list, candles_1h: list = None, pos: dict = None) -> tuple:
    if not candles_15m or len(candles_15m) < 25:
        return _round_px(current_price * 1.012), True, "step_init"

    dna = shadow.get_coin_dna(symbol)
    wave_mult = dna.get("wave_mult", 1.0)
    wave_class = dna.get("wave_class", "mid_wave")

    closes = [c["close"] for c in candles_15m]
    e21_series = ema(closes, 21)
    e21 = e21_series[-1]
    r15 = rsi(closes)
    a15 = atr(candles_15m)

    pos = pos or {}
    old_tp = float(pos.get("tp", 0.0))
    tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or current_price)
    max_p = max(float(pos.get("max_price", current_price)), current_price)

    pierce_atr_mult = 1.35 if wave_class == "long_wave" else (0.85 if wave_class == "short_wave" else 1.10)
    pierce_step = max(a15 * pierce_atr_mult * wave_mult, current_price * 0.0085)
    pierce_step = min(pierce_step, current_price * 0.0180)

    pullback_from_peak = max_p - current_price
    e21_falling = len(e21_series) >= 4 and e21 < e21_series[-3] * 0.9995

    if old_tp > current_price and current_price < e21 * 0.9985 and pullback_from_peak >= 0.80 * a15 and e21_falling:
        lowered_tp = max(
            tp1_ref * 1.0055,
            current_price + max(0.85 * a15, current_price * 0.0075),
            e21 + 0.95 * a15
        )
        if lowered_tp < old_tp * 0.9980:
            return _round_px(lowered_tp), True, "step_down_for_pierce"

    ideal_pierce_tp = max(
        current_price + pierce_step,
        e21 + (1.60 * wave_mult) * a15
    )

    if old_tp <= current_price:
        return _round_px(ideal_pierce_tp), True, "step_up_pierce"

    if (old_tp - current_price) <= 0.35 * a15 and r15 >= 55 and current_price >= e21:
        stepped_up = max(old_tp * 1.0055, current_price + pierce_step * 0.90)
        return _round_px(stepped_up), True, "step_up_wave"

    return _round_px(old_tp), False, "step_hold"


def update_position_sl(pos: dict, max_p: float, last_price: float = None, candles_15m: list = None) -> tuple:
    avg_p = pos.get("avg", 0.0)
    if avg_p <= 0:
        return False, pos.get("sl"), pos.get("sl_state", "SHIELD"), ""

    last_p = last_price or max_p
    sym = pos.get("symbol", "")
    dna = shadow.get_coin_dna(sym) if sym else {"wave_class": "mid_wave", "wave_mult": 1.0}
    wave_class = pos.get("wave_class") or dna.get("wave_class", "mid_wave")

    tier = pos.get("tier") or "SMALL"
    cfg = TIER_TRAIL_CONFIG.get(tier, TIER_TRAIL_CONFIG["SMALL"])

    current_sl = _round_px(pos.get("sl", 0.0))
    new_sl = current_sl
    current_state = pos.get("sl_state", "SHIELD")
    new_state = current_state

    breakeven = calc_breakeven_price(avg_p)
    peak_profit_pct = (max_p - avg_p) / avg_p * 100.0
    curr_profit_pct = (last_p - avg_p) / avg_p * 100.0
    tp_price = float(pos.get("tp", 0.0))
    init_tp = float(pos.get("initial_tp") or tp_price)
    tp_target_pct = ((init_tp - avg_p) / avg_p * 100.0) if init_tp > avg_p else 1.5

    closed_low = 0.0
    prev_closed_low = 0.0
    if candles_15m and len(candles_15m) >= 50:
        closes = [c["close"] for c in candles_15m]
        pos["_ema21"] = ema(closes, 21)[-1]
        pos["_ema50"] = ema(closes, 50)[-1]
        pos["_atr15"] = atr(candles_15m)
        pos["_rsi15"] = rsi(closes)
        closed_low = candles_15m[-2]["low"]
        prev_closed_low = min(candles_15m[-2]["low"], candles_15m[-3]["low"])

    e21 = pos.get("_ema21", 0.0)
    e50 = pos.get("_ema50", 0.0)
    a15 = pos.get("_atr15", 0.0)
    r15 = pos.get("_rsi15", 50.0)
    now_ts = int(time.time())
    pos_age = now_ts - int(pos.get("entry_time", now_ts))

    is_climax = (r15 >= 77) or (a15 > 0 and e21 > 0 and (last_p - e21) > 3.0 * a15)

    if pos.get("tp1_done"):
        tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or 0.0)
        tp1_pct = ((tp1_ref - avg_p) / avg_p * 100.0) if tp1_ref > avg_p else tp_target_pct

        runner_floor = _round_px(max(breakeven, avg_p * 1.0025))

        if peak_profit_pct >= tp1_pct + 0.85:
            mid_lock = _round_px(avg_p + (tp1_ref - avg_p) * 0.55) if tp1_ref > avg_p else _round_px(avg_p * 1.0080)
            runner_floor = max(runner_floor, mid_lock)

        if tp1_ref > 0 and peak_profit_pct >= tp1_pct + 1.35:
            runner_floor = max(runner_floor, _round_px(tp1_ref * 1.0010))

        wave_step_sl = 0.0
        if a15 > 0 and e21 > 0 and e50 > 0:
            if is_climax:
                candle_anchor = (closed_low - 0.15 * a15) if closed_low > 0 else (last_p - 0.55 * a15)
                wave_step_sl = max(e21, candle_anchor, last_p * 0.991)
            elif peak_profit_pct < tp1_pct + 1.20:
                wave_step_sl = min(e50 - 0.40 * a15, last_p - 1.15 * a15)
            else:
                anchor = (0.45 * e50 + 0.55 * e21) if wave_class == "long_wave" else e21
                wave_step_sl = anchor - 0.45 * a15

        dist_mult = 1.15 if wave_class == "long_wave" else (0.80 if wave_class == "short_wave" else 0.95)
        if peak_profit_pct >= tp1_pct + 1.5:
            dist_mult *= 0.80
        runner_trail = _round_px(max_p * (1.0 - cfg["runner_dist"] * dist_mult))

        raw_candidate = _round_px(max(breakeven, runner_floor, runner_trail, wave_step_sl))
        if raw_candidate >= last_p * 0.9930:
            raw_candidate = _round_px(last_p * 0.9920)

        if raw_candidate > new_sl * 1.0008 and last_p > raw_candidate * 1.0025:
            new_sl = raw_candidate

        if tp1_ref > 0 and new_sl >= tp1_ref * 0.9995:
            new_state = "ABOVE_TP1"
        elif new_sl > breakeven * 1.0025:
            new_state = "TRAILING_RUNNER"
        else:
            new_state = "BREAKEVEN"
    else:
        shield_step_trigger = max(0.75, tp_target_pct * 0.60)
        if curr_profit_pct >= shield_step_trigger and a15 > 0 and e50 > 0:
            step_shield = _round_px(min(avg_p * 0.9975, max(e50 - 0.50 * a15, prev_closed_low - 0.30 * a15)))
            if step_shield > new_sl * 1.0015 and last_p > step_shield * 1.004:
                new_sl = step_shield
                new_state = "SHIELD"

        be_trigger = max(0.95, tp_target_pct * 0.72)
        if curr_profit_pct >= be_trigger and breakeven > new_sl and last_p > breakeven * 1.0035:
            new_sl = breakeven
            new_state = "BREAKEVEN"

        if pos_age >= 3600 and e21 > 0 and e50 > 0 and e21 < e50 * 0.998 and last_p < e50 * 0.998 and closed_low > 0 and a15 > 0:
            tight_broken_sl = _round_px(closed_low - 0.25 * a15)
            if tight_broken_sl > new_sl * 1.002 and last_p > tight_broken_sl * 1.003:
                new_sl = tight_broken_sl

        if is_climax and curr_profit_pct >= cfg["lock_trigger_pct"]:
            lock_sl = _round_px(max(breakeven, last_p * (1.0 - cfg["lock_dist"])))
            if lock_sl > new_sl and last_p > lock_sl * 1.0025:
                new_sl = lock_sl
                new_state = "PROFIT_LOCK"

    new_sl = _round_px(new_sl)

    if (new_sl > current_sl + 1e-9) or (new_state != current_state and new_sl >= current_sl):
        tag_desc = sl_emoji(new_state)
        return True, new_sl, new_state, tag_desc

    return False, current_sl, current_state, ""
