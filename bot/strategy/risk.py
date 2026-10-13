from bot.strategy.indicators import atr, find_sr_levels, get_all_sr_levels
from bot.strategy.shadow import shadow

TIER_SL_FLOOR = {
    "TOP20": 0.009,  # 0.9%
    "MID":   0.012,  # 1.2%
    "SMALL": 0.015,  # 1.5%
    "MICRO": 0.020,  # 2.0%
}

# Реалистичный пол для первого тейка (TP1 50%) на 15m ТФ (остаток 50% идет по 1H уровням)
TIER_TP_FLOOR = {
    "TOP20": 0.010,  # 1.0%
    "MID":   0.013,  # 1.3%
    "SMALL": 0.016,  # 1.6%
    "MICRO": 0.018,  # 1.8%
}

TIER_MAX_SL = {
    "TOP20": 2.0,   # Макс стоп 2.0%
    "MID":   2.6,   # Макс стоп 2.6%
    "SMALL": 3.2,   # Макс стоп 3.2%
    "MICRO": 4.0,   # Макс стоп 4.0%
}

TIER_TRAIL_CONFIG = {
    "TOP20": {"be_trigger": 0.007, "runner_dist": 0.008, "pre_tp_trigger": 1.2, "pre_tp_dist": 0.005},
    "MID":   {"be_trigger": 0.009, "runner_dist": 0.012, "pre_tp_trigger": 1.5, "pre_tp_dist": 0.008},
    "SMALL": {"be_trigger": 0.011, "runner_dist": 0.015, "pre_tp_trigger": 1.8, "pre_tp_dist": 0.010},
    "MICRO": {"be_trigger": 0.013, "runner_dist": 0.020, "pre_tp_trigger": 2.0, "pre_tp_dist": 0.014},
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
    """Точный математический безубыток с учетом двойной комиссии Bybit + микро-запас."""
    if avg_price <= 0:
        return 0.0
    f = fee_pct / 100.0
    be = avg_price * (1.0 + f) / (1.0 - f) * 1.0005
    return round(be, 8)


def evaluate_setup_grade(cand: dict) -> str:
    """Оценивает класс сетапа (Grade A+ vs B) по степени схождения факторов."""
    confluence = 0
    patterns = cand.get("patterns", [])
    keys = cand.get("reason_keys", [])
    sv = cand.get("signal_values", {})

    if len(patterns) >= 2:
        confluence += 2
    elif len(patterns) == 1:
        confluence += 1

    if cand.get("sup_touches", 0) >= 2 or "support_bounce" in keys:
        confluence += 1

    if "mtf_dip" in keys or sv.get("volume", 1.0) >= 1.35:
        confluence += 1

    if cand.get("score", 0.0) >= 7.0:
        confluence += 1

    return "A+" if confluence >= 3 else "B"


def calc_total_open_risk_pct(positions: dict, orders: list, equity: float) -> float:
    """Считает текущий открытый риск портфеля (Portfolio VaR) по активным стопам."""
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
                        min_rr: float = None):
    tier = tier or "SMALL"
    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.015)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.016)
    max_sl_allowed = TIER_MAX_SL.get(tier, 3.2)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    # 1. Расчет защитного стоп-лосса (SL)
    # Привязываем стоп к поддержке ТОЛЬКО если она в рабочей ближней зоне (<= 1.8 ATR)
    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry and (entry - sup) <= 1.8 * a:
        struct_sl = round(sup * 0.997, 8)
        candidate_dist = max(entry - struct_sl, entry * sl_floor_pct)
        candidate_dist_pct = (candidate_dist / entry) * 100
        if candidate_dist_pct <= max_sl_allowed:
            sl_dist = candidate_dist
            sl = round(entry - sl_dist, 8)

    # Если поддержка далеко внизу — ставим компактный волатильностный стоп по ATR
    if sl_dist <= 0:
        base_mult = 0.75 if is_mom else (1.55 if entry_mode == "reversal" else 1.15)
        sl_dist_raw = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
        sl_dist = min(sl_dist_raw, entry * (max_sl_allowed / 100.0))
        sl = round(entry - sl_dist, 8)

    if sl >= entry or (sl_dist / entry * 100) > max_sl_allowed:
        return None, None, False, "Недопустимая дистанция SL"

    # 2. Расчет первого тейк-профита (TP1 — фиксация 50% и перевод в БУ)
    tp_dist_raw = max(1.65 * a * tp_tuning, sl_dist * 1.35, entry * tp_floor_pct)
    tp = round(entry + tp_dist_raw, 8)
    if min_rr is None:
        min_rr = 1.05 if entry_mode == "sniper" else 0.95

    # Проверка капитальной стены сопротивления (>= 3 касания)
    if res and res > entry and res_touches >= 3:
        res_dist_pct = (res - entry) / entry * 100
        if res_dist_pct < 0.35:
            return None, None, False, f"Капитальная стена сопротивления в упор ({res_dist_pct:.2f}% < 0.35%)"

        res_cap_tp = round(res * 0.997, 8)
        if res_cap_tp < tp:
            if (res_cap_tp - entry) / sl_dist >= 0.85:
                tp = res_cap_tp
                min_rr = min(min_rr, 0.85)
            else:
                return None, None, False, f"Капитальная стена не дает забрать 0.85x RR (RR={(res_cap_tp - entry)/sl_dist:.2f})"

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
    avg_p = pos.get("avg", 0.0)
    if avg_p <= 0:
        return False, pos.get("sl"), pos.get("sl_state", "SHIELD"), ""

    tier = pos.get("tier") or "SMALL"
    cfg = TIER_TRAIL_CONFIG.get(tier, TIER_TRAIL_CONFIG["SMALL"])

    current_sl = round(float(pos.get("sl", 0.0)), 8)
    new_sl = current_sl
    current_state = pos.get("sl_state", "SHIELD")
    new_state = current_state

    breakeven = calc_breakeven_price(avg_p)
    profit_pct = (max_p - avg_p) / avg_p * 100

    if pos.get("tp1_done"):
        new_state = "TRAILING_RUNNER"
        runner_trail = round(max_p * (1 - cfg["runner_dist"]), 8)
        min_runner_sl = round(avg_p * 1.005, 8)
        new_sl = max(new_sl, runner_trail, min_runner_sl)
    else:
        if profit_pct >= cfg["pre_tp_trigger"]:
            new_state = "PROFIT_LOCK"
            new_sl = max(new_sl, round(max_p * (1 - cfg["pre_tp_dist"]), 8))
        elif max_p >= avg_p * (1 + cfg["be_trigger"]):
            if new_sl < breakeven:
                new_state = "BREAKEVEN"
                new_sl = breakeven

    new_sl = round(new_sl, 8)

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
