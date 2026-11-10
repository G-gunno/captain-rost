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
    "SMALL": 2.3,
    "MICRO": 2.5,
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"runner_dist": 0.014, "lock_trigger_pct": 1.85, "lock_dist": 0.006},
    "MID":   {"runner_dist": 0.017, "lock_trigger_pct": 2.05, "lock_dist": 0.007},
    "SMALL": {"runner_dist": 0.020, "lock_trigger_pct": 2.25, "lock_dist": 0.008},
    "MICRO": {"runner_dist": 0.023, "lock_trigger_pct": 2.50, "lock_dist": 0.009},
}

TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 8.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 0.95,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 10.0,
        "risk_per_trade_core": 1.50,
        "risk_per_trade_alt": 1.20,
        "min_rr_sniper": 0.94,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 12.0,
        "risk_per_trade_core": 1.75,
        "risk_per_trade_alt": 1.45,
        "min_rr_sniper": 0.92,
        "min_rr_alt": 0.84,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 16.0,
        "risk_per_trade_core": 2.10,
        "risk_per_trade_alt": 1.75,
        "min_rr_sniper": 0.90,
        "min_rr_alt": 0.82,
        "require_grade_a_for_alt": False,
    },
}


def _round_px(val: float) -> float:
    """Точное округление для любых цен, включая микро-монеты вроде NFTUSDT ($0.000000196)."""
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

    if entry_mode == "sniper" and pos_in_48h > 0.66 and gate_1h != "1H_SUPER_TREND":
        return "B"
    if entry_mode == "reversal" and pos_in_48h > 0.40:
        return "B"
    if corr > 0.78 and entry_mode != "reversal":
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

    if gate_1h in ("1H_TREND_OK", "1H_SUPER_TREND") and ("mtf_dip" in keys or pos_in_48h <= 0.60):
        confluence += 1
    elif gate_1h == "1H_SUPPORT_ONLY" and pos_in_48h <= 0.38 and sup_touches >= 3:
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
    dna = shadow.get_coin_dna(symbol)
    wave_mult = dna.get("wave_mult", 1.0)

    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.015)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.015)
    # Адаптируем максимальный SL под ДНК волатильности монеты (до 2.65% для высоковолатильных ракет вроде TA, BR)
    base_max_sl = TIER_MAX_SL.get(tier, 2.3)
    max_sl_allowed = round(base_max_sl * (1.12 if wave_mult > 1.1 else 1.0), 2)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    base_mult = 0.85 if is_mom else (1.25 if entry_mode == "reversal" else 1.18)
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
        min_rr = 0.94 if entry_mode == "sniper" else 0.84

    tp_atr_mult = 1.28 if entry_mode == "reversal" else (1.55 if is_mom else 1.35)
    rr_target = 1.06 if entry_mode == "reversal" else max(1.10, min_rr + 0.04)
    tp_dist_raw = max(tp_atr_mult * a * tp_tuning, sl_dist * rr_target, entry * tp_floor_pct)

    # Масштабируем потолок TP1 с учётом SL и ДНК волны (до 2.55%), чтобы убрать отказы RR 0.89 < 0.95 (BR, TA, NXPC)
    if not is_mom:
        max_tp1_cap = max(entry * (0.0225 * wave_mult), sl_dist * max(1.02, min_rr + 0.02))
        tp_dist_raw = min(tp_dist_raw, max_tp1_cap)

    tp = _round_px(entry + tp_dist_raw)

    if entry_mode == "reversal" and e21_1h and e50_1h and e21_1h < e50_1h:
        if e21_1h > entry * 1.0095:
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
            if wall_rr >= 0.68 or wall_gain_pct >= 0.82:
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
    Непрерывная волновая огибающая потолка TP🏃🏻‍➡️:
    Плавно скользит впереди цены по волне EMA21 + k_wave * ATR15 с учётом ДНК длины волны монеты.
    """
    if not candles_15m or len(candles_15m) < 25:
        return _round_px(current_price * 1.045), True, "wave_static"

    dna = shadow.get_coin_dna(symbol)
    wave_mult = dna.get("wave_mult", 1.0)
    wave_amp_pct = dna.get("wave_amp_pct", 3.5)

    closes = [c["close"] for c in candles_15m]
    e21_series = ema(closes, 21)
    e50_series = ema(closes, 50)
    e21 = e21_series[-1]
    e50 = e50_series[-1]
    r15 = rsi(closes)
    a15 = atr(candles_15m)

    dist_from_e21_atr = ((current_price - e21) / a15) if a15 > 0 else 0.0
    if r15 >= 79 or dist_from_e21_atr > 3.8:
        climax_tp = _round_px(max(current_price, candles_15m[-1]["high"]) * 1.018)
        return climax_tp, False, "climax_freeze"

    recent_box = (max(c["high"] for c in candles_15m[-6:]) - min(c["low"] for c in candles_15m[-6:])) / current_price if current_price > 0 else 0.0
    if recent_box <= 0.011 and e21 <= e50 * 0.999:
        old_tp = (pos or {}).get("tp", current_price * 1.025)
        return _round_px(max(old_tp, current_price * 1.018)), False, "shelf_freeze"

    e21_slope_up = len(e21_series) >= 4 and e21 > e21_series[-4] * 1.001
    base_k = 4.2 if (e21 > e50 and e21_slope_up and r15 >= 54) else 3.0
    k_wave = base_k * wave_mult

    wave_envelope = max(
        e21 + k_wave * a15,
        current_price + (2.4 * wave_mult) * a15,
        current_price * (1.0 + max(0.024, min(0.065, wave_amp_pct / 100.0 * 0.75)))
    )

    all_res, _ = get_all_sr_levels(candles_15m, candles_1h)
    res_above = [r[0] for r in all_res if r[0] > current_price * 1.022 and r[1] >= 6]
    if res_above:
        res_above.sort()
        wall = res_above[0] * 0.997
        if wall > current_price * 1.020:
            wave_envelope = min(wave_envelope, wall)

    old_tp = float((pos or {}).get("tp", 0.0))
    if old_tp > current_price:
        # Плавное волновое сглаживание потолка без резких ступенек
        target = 0.65 * wave_envelope + 0.35 * old_tp
        target = max(target, current_price * 1.018)
    else:
        target = wave_envelope

    return _round_px(target), True, "wave_glide"


def update_position_sl(pos: dict, max_p: float, last_price: float = None, candles_15m: list = None) -> tuple:
    """
    4-фазный Волновой Стоп (Dynamic Wave SL):
    - Фаза 1 (До TP1): структурный стоп; перевод в БУ по времени только при ослаблении тренда под EMA50.
    - Фаза 2 (Ретест после TP1): свободный буфер +0.35% от входа (защита от вылета на первом откате).
    - Фаза 3 (Развитая волна): плавная волна под EMA50/EMA21 + двухступенчатый пол (+0.80% 🛡📈 -> выше TP1 🛡🔥).
    - Фаза 4 (Парабола RSI >= 78): выход НАД EMA прямо под минимумы свечей (Candle Low Trail).
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
    is_rocket = (entry_mode == "rocket" or pos.get("is_momentum", False))
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
    tp_target_pct = ((init_tp - avg_p) / avg_p * 100.0) if init_tp > avg_p else 1.6

    closed_low = 0.0
    if candles_15m and len(candles_15m) >= 50:
        closes = [c["close"] for c in candles_15m]
        pos["_ema21"] = ema(closes, 21)[-1]
        pos["_ema50"] = ema(closes, 50)[-1]
        pos["_atr15"] = atr(candles_15m)
        pos["_rsi15"] = rsi(closes)
        closed_low = candles_15m[-2]["low"]

    e21 = pos.get("_ema21", 0.0)
    e50 = pos.get("_ema50", 0.0)
    a15 = pos.get("_atr15", 0.0)
    r15 = pos.get("_rsi15", 50.0)

    # Фаза 4: Параболическая кульминация (выход НАД EMA прямо под лой свечи)
    is_climax = (r15 >= 78) or (a15 > 0 and e21 > 0 and (last_p - e21) > 3.2 * a15)

    wave_ema_sl = 0.0
    if a15 > 0 and e21 > 0 and e50 > 0:
        if is_climax and curr_profit_pct >= 1.6:
            candle_anchor = (closed_low - 0.20 * a15) if closed_low > 0 else (last_p - 0.65 * a15)
            wave_ema_sl = max(e21, candle_anchor, last_p * 0.990)
        elif pos.get("tp1_done"):
            # Волновая опора после TP1: плавно переходит от EMA50 к EMA21 по мере роста профита и класса волны
            if wave_class == "short_wave" or entry_mode == "reversal":
                if last_p > e21 and peak_profit_pct >= tp_target_pct + 0.35:
                    wave_ema_sl = e21 - 0.45 * a15
                elif last_p > e50:
                    wave_ema_sl = e50 - 0.45 * a15
            elif wave_class == "long_wave" and not is_rocket:
                # Дальнобойщики (INIT, MET, PEPE) едут по широкой волне EMA50 - 0.35*ATR, не вылетая на локальных откатах
                if last_p > e50 and peak_profit_pct >= tp_target_pct + 0.60:
                    blend = min(0.65, max(0.0, (peak_profit_pct - tp_target_pct) / 8.0))
                    anchor_ema = (1.0 - blend) * e50 + blend * e21
                    wave_ema_sl = anchor_ema - 0.40 * a15
            else:
                if last_p > e21 and peak_profit_pct >= tp_target_pct + 0.75:
                    wave_ema_sl = (0.5 * e50 + 0.5 * e21) - 0.45 * a15
                elif last_p > e50 and peak_profit_pct >= tp_target_pct + 0.35:
                    wave_ema_sl = e50 - 0.50 * a15

        if wave_ema_sl >= last_p * 0.993:
            wave_ema_sl = last_p * 0.992

    if pos.get("tp1_done"):
        tp1_ref = float(pos.get("tp1_price") or pos.get("initial_tp") or 0.0)
        tp1_pct = ((tp1_ref - avg_p) / avg_p * 100.0) if tp1_ref > avg_p else tp_target_pct

        # Ступень 1 (Фаза 2 — буфер ретеста сразу после TP1): +0.35% от входа
        runner_floor = _round_px(max(breakeven, avg_p * 1.0035))

        # Ступень 2 (Фаза 3А — цена прошла ещё +0.42% выше TP1): поднимаем пол на +0.80% от входа (🛡📈)
        if peak_profit_pct >= tp1_pct + 0.42:
            mid_lock = _round_px(avg_p + (tp1_ref - avg_p) * 0.58) if tp1_ref > avg_p else _round_px(avg_p * 1.0080)
            runner_floor = max(runner_floor, mid_lock)

        # Ступень 3 (Фаза 3Б — цена оторвалась на +0.85% выше TP1): стоп переходит ЗА линию TP1 (🛡🔥)
        if tp1_ref > 0 and ((a15 > 0 and max_p >= tp1_ref + 0.85 * a15) or peak_profit_pct >= tp1_pct + 0.85):
            runner_floor = max(runner_floor, _round_px(tp1_ref * 1.0008))

        # Адаптивная ширина волнового отступа от пика
        dist_mult = 1.12 if wave_class == "long_wave" else (0.78 if wave_class == "short_wave" else 0.95)
        if peak_profit_pct >= tp1_pct + 1.2:
            dist_mult *= 0.82
        runner_dist = cfg["runner_dist"] * dist_mult
        runner_trail = _round_px(max_p * (1.0 - runner_dist))

        raw_candidate = max(breakeven, runner_floor, runner_trail, wave_ema_sl)
        # Сглаживаем шаг подъёма стопа, чтобы он шёл плавной волной, а не резкими прыжками
        if raw_candidate > new_sl and last_p > raw_candidate * 1.0025:
            if current_sl >= breakeven and not is_climax:
                smoothed_sl = _round_px(0.45 * current_sl + 0.55 * raw_candidate)
                new_sl = max(runner_floor, smoothed_sl) if last_p > runner_floor * 1.002 else smoothed_sl
            else:
                new_sl = raw_candidate

        if tp1_ref > 0 and new_sl >= tp1_ref * 0.9995:
            new_state = "ABOVE_TP1"
        elif new_sl > breakeven * 1.0030:
            new_state = "TRAILING_RUNNER"
        else:
            new_state = "BREAKEVEN"
    else:
        # ДО TP1:
        # 1. Защита от застрявших сделок через 90 мин срабатывает ТОЛЬКО если тренд под EMA50 ослаб!
        # Живые тренды над EMA50 (PEPE, TA, CAP, BIRB) НЕ выбиваем в БУ раньше времени!
        pos_age = int(time.time()) - int(pos.get("entry_time", int(time.time())))
        trend_weakened = (e50 > 0 and last_p < e50) or (e21 > 0 and e50 > 0 and e21 < e50 * 0.999)
        if pos_age >= 5400 and trend_weakened and curr_profit_pct >= 0.60 and breakeven > new_sl:
            new_sl = breakeven
            new_state = "BREAKEVEN"

        # 2. Стандартный перевод в БУ (🛡🔒) только когда ТЕКУЩАЯ цена прошла 78% пути к TP1 (и не менее +1.20%)
        be_trigger = max(1.20, tp_target_pct * 0.78)
        if curr_profit_pct >= be_trigger and breakeven > new_sl and last_p > breakeven * 1.0035:
            new_sl = breakeven
            new_state = "BREAKEVEN"

        # 3. Экстремальная свеча кульминации до TP1
        if is_climax and curr_profit_pct >= cfg["lock_trigger_pct"]:
            lock_sl = _round_px(max(breakeven, wave_ema_sl, last_p * (1.0 - cfg["lock_dist"])))
            if lock_sl > new_sl and last_p > lock_sl * 1.0025:
                new_sl = lock_sl
                new_state = "PROFIT_LOCK"

    new_sl = _round_px(new_sl)

    if (new_sl > current_sl + 1e-9) or (new_state != current_state and new_sl >= current_sl):
        tag_desc = sl_emoji(new_state)
        return True, new_sl, new_state, tag_desc

    return False, current_sl, current_state, ""
