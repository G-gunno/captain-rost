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
    "TOP20": 0.011,  # 1.1%
    "MID":   0.013,  # 1.3%
    "SMALL": 0.014,  # 1.4%
    "MICRO": 0.016,  # 1.6%
}

TIER_MAX_SL = {
    "TOP20": 2.0,
    "MID":   2.2,
    "SMALL": 2.3,
    "MICRO": 2.5,
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"runner_dist": 0.011, "lock_trigger_pct": 1.65, "lock_dist": 0.005},
    "MID":   {"runner_dist": 0.013, "lock_trigger_pct": 1.85, "lock_dist": 0.006},
    "SMALL": {"runner_dist": 0.015, "lock_trigger_pct": 2.05, "lock_dist": 0.007},
    "MICRO": {"runner_dist": 0.018, "lock_trigger_pct": 2.25, "lock_dist": 0.008},
}

TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 8.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 0.94,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 10.0,
        "risk_per_trade_core": 1.50,
        "risk_per_trade_alt": 1.20,
        "min_rr_sniper": 0.92,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 12.0,
        "risk_per_trade_core": 1.75,
        "risk_per_trade_alt": 1.45,
        "min_rr_sniper": 0.90,
        "min_rr_alt": 0.82,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 16.0,
        "risk_per_trade_core": 2.10,
        "risk_per_trade_alt": 1.75,
        "min_rr_sniper": 0.88,
        "min_rr_alt": 0.82,
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

    if entry_mode == "reversal" and pos_in_48h > 0.38:
        return "B"
    if corr > 0.86 and entry_mode != "reversal":
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

    if gate_1h in ("1H_TREND_OK", "1H_SUPER_TREND") and ("mtf_dip" in keys or pos_in_48h <= 0.75):
        confluence += 1
    elif gate_1h == "1H_SUPPORT_ONLY" and pos_in_48h <= 0.35 and sup_touches >= 3:
        confluence += 1

    if vol_ratio >= 1.25:
        confluence += 1

    if tier in ("TOP20", "MID") or corr < 0.40:
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
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.014)
    base_max_sl = TIER_MAX_SL.get(tier, 2.3)
    max_sl_allowed = round(base_max_sl * (1.12 if wave_mult > 1.1 else 1.0), 2)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    base_mult = 0.85 if is_mom else (1.22 if entry_mode == "reversal" else 1.15)
    raw_atr_sl_pct = (base_mult * a * sl_tuning / entry) * 100.0 if entry > 0 else 0.0

    if raw_atr_sl_pct > max_sl_allowed * 1.30:
        return None, None, False, f"Аномальная волатильность ATR (SL {raw_atr_sl_pct:.2f}% > {max_sl_allowed}%)"

    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry and (entry - sup) <= 1.8 * a:
        struct_sl = _round_px(sup * 0.997)
        candidate_dist = max(entry - struct_sl, entry * sl_floor_pct)
        if (candidate_dist / entry) * 100 <= max_sl_allowed:
            sl_dist = candidate_dist
            sl = _round_px(entry - sl_dist)

    if sl_dist <= 0:
        sl_dist_raw = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
        sl_dist = min(sl_dist_raw, entry * (max_sl_allowed / 100.0))
        sl = _round_px(entry - sl_dist)

    if recent_low_6 and recent_low_6 > 0 and recent_low_6 < entry:
        wick_safe_sl = _round_px(recent_low_6 * 0.997)
        if wick_safe_sl < sl:
            wick_dist_pct = (entry - wick_safe_sl) / entry * 100
            if wick_dist_pct <= max_sl_allowed:
                sl = wick_safe_sl
                sl_dist = entry - sl

    if ema50 and ema50 > 0 and entry > ema50:
        ema50_safe_sl = _round_px(ema50 - 0.55 * a)
        if sl > ema50_safe_sl and abs(sl - ema50) <= 0.55 * a:
            ema_dist_pct = (entry - ema50_safe_sl) / entry * 100
            if ema_dist_pct <= max_sl_allowed:
                sl = ema50_safe_sl
                sl_dist = entry - sl

    if sl >= entry or (sl_dist / entry * 100) > max_sl_allowed:
        return None, None, False, "Недопустимая дистанция SL"

    if min_rr is None:
        min_rr = 0.92 if entry_mode == "sniper" else 0.84

    tp_atr_mult = 1.22 if entry_mode == "reversal" else (1.45 if is_mom else 1.28)
    rr_target = 1.02 if entry_mode == "reversal" else max(1.04, min_rr + 0.04)
    tp_dist_raw = max(tp_atr_mult * a * tp_tuning, sl_dist * rr_target, entry * tp_floor_pct)

    if not is_mom:
        max_tp1_cap = max(entry * (0.021 * wave_mult), sl_dist * max(1.00, min_rr + 0.02))
        tp_dist_raw = min(tp_dist_raw, max_tp1_cap)

    tp = _round_px(entry + tp_dist_raw)

    if entry_mode == "reversal" and e21_1h and e50_1h and e21_1h < e50_1h:
        if e21_1h > entry * 1.009:
            cap_e21 = _round_px(e21_1h * 0.9985)
            if cap_e21 < tp:
                tp = cap_e21
                min_rr = min(min_rr, (tp - entry) / sl_dist)
        elif e50_1h > entry * 1.010:
            cap_e50 = _round_px(e50_1h * 0.998)
            if cap_e50 < tp:
                tp = cap_e50
                min_rr = min(min_rr, (tp - entry) / sl_dist)

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
    """
    Частый Ступенчатый Тейк «В РАСЧЁТЕ НА ПРОКОЛ» (Wick-Pierce Step Target):
    - Не улетает в луну (+5%), а держится в зоне досягаемости импульсного прокола (+0.65%..+1.35% от цены).
    - Двигается ступеньками каждую минуту:
      * Если свечи давят вверх — шагает ступеньками вверх (+0.20%..+0.35%).
      * Если цена топчется или откатила от пика — спускается ступеньками ВНИЗ ближе к цене, чтобы поймать следующий прокол!
    """
    if not candles_15m or len(candles_15m) < 25:
        return _round_px(current_price * 1.010), True, "step_init"

    dna = shadow.get_coin_dna(symbol)
    wave_mult = dna.get("wave_mult", 1.0)
    wave_class = dna.get("wave_class", "mid_wave")

    closes = [c["close"] for c in candles_15m]
    e21_series = ema(closes, 21)
    e50_series = ema(closes, 50)
    e21 = e21_series[-1]
    e50 = e50_series[-1]
    r15 = rsi(closes)
    a15 = atr(candles_15m)

    pos = pos or {}
    old_tp = float(pos.get("tp", 0.0))
    tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or current_price)
    max_p = max(float(pos.get("max_price", current_price)), current_price)

    # Дистанция на прокол шпилькой (в зависимости от класса волны монеты: от +0.65% до +1.35%)
    pierce_atr_mult = 1.15 if wave_class == "long_wave" else (0.72 if wave_class == "short_wave" else 0.90)
    pierce_step = max(a15 * pierce_atr_mult * wave_mult, current_price * 0.0065)
    pierce_step = min(pierce_step, current_price * 0.0155)

    # 1. Если цена откатила от локального пика или импульс остывает — ОПУСКАЕМ ступеньку TP ниже (под прокол)!
    pullback_from_peak = max_p - current_price
    e21_flattening = len(e21_series) >= 3 and e21 <= e21_series[-3] * 1.0005

    if old_tp > current_price and (pullback_from_peak >= 0.30 * a15 or e21_flattening or r15 < 56):
        lowered_tp = max(
            tp1_ref * 1.0035,
            current_price + max(0.65 * a15, current_price * 0.0055),
            e21 + 1.05 * a15
        )
        if lowered_tp < old_tp * 0.9985:
            return _round_px(lowered_tp), True, "step_down_for_pierce"

    # 2. Если свечи идут вверх — ставим/поднимаем ступеньку TP на расстояние одного импульсного прокола
    ideal_pierce_tp = max(
        current_price + pierce_step,
        e21 + (1.45 * wave_mult) * a15
    )

    all_res, _ = get_all_sr_levels(candles_15m, candles_1h)
    res_above = [r[0] for r in all_res if current_price * 1.004 < r[0] < ideal_pierce_tp and r[1] >= 4]
    if res_above:
        res_above.sort()
        ideal_pierce_tp = min(ideal_pierce_tp, res_above[0] * 0.9985)

    if old_tp <= current_price:
        return _round_px(ideal_pierce_tp), True, "step_up_pierce"

    # Если цена подошла близко к текущему TP (< 0.30*ATR) на сильном импульсе — шагаем на +1 ступеньку вверх
    if (old_tp - current_price) <= 0.30 * a15 and r15 >= 58 and current_price > e21:
        stepped_up = max(old_tp * 1.0035, current_price + pierce_step * 0.85)
        return _round_px(stepped_up), True, "step_up_wave"

    return _round_px(old_tp), False, "step_hold"


def update_position_sl(pos: dict, max_p: float, last_price: float = None, candles_15m: list = None) -> tuple:
    """
    Частый Ступенчатый Стоп (Frequent Step-Wave SL):
    - Двигается дискретными ступеньками (от +0.10%) каждую минуту вслед за ростом цены, EMA и локальных лоёв.
    - До TP1: плавно поджимает начальный стоп вдоль растущей EMA50 без раннего удушения в БУ (фикс NFTUSDT).
    - После TP1: держит буфер ретеста в первые 15 мин (фикс GRAMUSDT), а затем плотными ступеньками идёт под волной.
    """
    avg_p = pos.get("avg", 0.0)
    if avg_p <= 0:
        return False, pos.get("sl"), pos.get("sl_state", "SHIELD"), ""

    last_p = last_price or max_p
    sym = pos.get("symbol", "")
    dna = shadow.get_coin_dna(sym) if sym else {"wave_class": "mid_wave", "wave_mult": 1.0}
    wave_class = pos.get("wave_class") or dna.get("wave_class", "mid_wave")

    tier = pos.get("tier") or "SMALL"
    entry_mode = pos.get("entry_mode", "sniper")
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
        tp1_age = now_ts - int(pos.get("tp1_time", now_ts))

        # Базовый пол сразу после TP1: +0.35% от входа
        runner_floor = _round_px(max(breakeven, avg_p * 1.0035))

        # Если цена прошла ещё +0.35% выше TP1 — поднимаем пол ступенькой на +0.75% от входа
        if peak_profit_pct >= tp1_pct + 0.35:
            mid_lock = _round_px(avg_p + (tp1_ref - avg_p) * 0.60) if tp1_ref > avg_p else _round_px(avg_p * 1.0075)
            runner_floor = max(runner_floor, mid_lock)

        # Если цена оторвалась на +0.70% выше TP1 — пол шагает ЗА линию TP1 (🛡🔥)
        if tp1_ref > 0 and ((a15 > 0 and max_p >= tp1_ref + 0.70 * a15) or peak_profit_pct >= tp1_pct + 0.70):
            runner_floor = max(runner_floor, _round_px(tp1_ref * 1.0010))

        # В первые 18 минут после TP1 держим буфер ретеста под EMA50 (чтобы не выбило тенью GRAM перед ракетой +10.8%),
        # а затем плотными ступеньками идём под волной EMA21 / лоями свечей
        wave_step_sl = 0.0
        if a15 > 0 and e21 > 0 and e50 > 0:
            if is_climax:
                candle_anchor = (closed_low - 0.15 * a15) if closed_low > 0 else (last_p - 0.55 * a15)
                wave_step_sl = max(e21, candle_anchor, last_p * 0.991)
            elif tp1_age < 1080 and wave_class == "long_wave" and peak_profit_pct < tp1_pct + 1.0:
                wave_step_sl = min(e50 - 0.35 * a15, last_p - 0.85 * a15)
            else:
                anchor = (0.4 * e50 + 0.6 * e21) if wave_class == "long_wave" else e21
                wave_step_sl = anchor - 0.35 * a15
                if prev_closed_low > wave_step_sl and last_p > prev_closed_low * 1.003:
                    wave_step_sl = prev_closed_low - 0.15 * a15

        dist_mult = 1.05 if wave_class == "long_wave" else (0.75 if wave_class == "short_wave" else 0.90)
        if peak_profit_pct >= tp1_pct + 1.0:
            dist_mult *= 0.80
        runner_trail = _round_px(max_p * (1.0 - cfg["runner_dist"] * dist_mult))

        raw_candidate = _round_px(max(breakeven, runner_floor, runner_trail, wave_step_sl))
        if raw_candidate >= last_p * 0.994:
            raw_candidate = _round_px(last_p * 0.993)

        # Ступенька фиксируется при любом сдвиге вверх >= 0.08%
        if raw_candidate > new_sl * 1.0008 and last_p > raw_candidate * 1.0020:
            new_sl = raw_candidate

        if tp1_ref > 0 and new_sl >= tp1_ref * 0.9995:
            new_state = "ABOVE_TP1"
        elif new_sl > breakeven * 1.0025:
            new_state = "TRAILING_RUNNER"
        else:
            new_state = "BREAKEVEN"
    else:
        # ДО TP1: частые ступеньки поджатия риска вдоль растущей волны (БЕЗ удушения живого тренда в БУ на 90-й минуте!)
        if pos_age >= 1200 and curr_profit_pct >= 0.25 and a15 > 0 and e50 > 0:
            # Плавное ступенчатое подтягивание начального стопа под растущую EMA50 и свинги
            step_shield = min(avg_p * 0.9975, e50 - 0.60 * a15)
            if prev_closed_low > 0 and prev_closed_low < avg_p:
                step_shield = max(step_shield, prev_closed_low - 0.25 * a15)
            step_shield = _round_px(step_shield)
            if step_shield > new_sl * 1.0012 and last_p > step_shield * 1.004:
                new_sl = step_shield
                new_state = "SHIELD"

        # Если же тренд до TP1 реально сломался (EMA21 ушла под EMA50 на 15m, как на PYTH в 20:30),
        # поджимаем стоп ступенькой под лой закрытой свечи, чтобы не отдавать полный стоп -1.8%!
        if pos_age >= 2700 and e21 > 0 and e50 > 0 and e21 < e50 * 0.9985 and last_p < e50 and closed_low > 0 and a15 > 0:
            tight_broken_sl = _round_px(closed_low - 0.20 * a15)
            if tight_broken_sl > new_sl * 1.0015 and last_p > tight_broken_sl * 1.002:
                new_sl = tight_broken_sl

        # Перевод в БУ (🛡🔒) до TP1 только при уверенном ТЕКУЩЕМ плюсе >= 0.90% (или 70% пути к TP1)
        be_trigger = max(0.90, tp_target_pct * 0.70)
        if curr_profit_pct >= be_trigger and breakeven > new_sl and last_p > breakeven * 1.0035:
            new_sl = breakeven
            new_state = "BREAKEVEN"

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
