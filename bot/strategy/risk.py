from bot.strategy.indicators import atr, find_sr_levels, get_all_sr_levels
from bot.strategy.shadow import shadow

FEE_SPOT_PCT = 0.10  # Bybit Spot Taker/Maker fee 0.1%

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
    "TOP20": {"be_trigger": 0.006, "runner_dist": 0.008, "pre_tp_trigger": 1.5, "pre_tp_dist": 0.006},
    "MID":   {"be_trigger": 0.009, "runner_dist": 0.013, "pre_tp_trigger": 2.0, "pre_tp_dist": 0.010},
    "SMALL": {"be_trigger": 0.011, "runner_dist": 0.016, "pre_tp_trigger": 2.2, "pre_tp_dist": 0.012},
    "MICRO": {"be_trigger": 0.013, "runner_dist": 0.022, "pre_tp_trigger": 2.5, "pre_tp_dist": 0.018},
}

# ⚡ ТОРГОВЫЕ ПОЛИТИКИ (Без искусственных ограничений на Other!)
TRADE_POLICIES = {
    "STRICT": {
        "max_portfolio_risk_pct": 1.5,
        "risk_per_trade_core": 0.50,
        "risk_per_trade_alt": 0.25,
        "min_rr_sniper": 1.35,
        "min_rr_alt": 1.15,
        "require_grade_a_for_alt": True,
    },
    "CAUTIOUS": {
        "max_portfolio_risk_pct": 2.0,
        "risk_per_trade_core": 0.75,
        "risk_per_trade_alt": 0.50,
        "min_rr_sniper": 1.25,
        "min_rr_alt": 1.10,
        "require_grade_a_for_alt": True,
    },
    "NORMAL": {
        "max_portfolio_risk_pct": 3.0,
        "risk_per_trade_core": 1.00,
        "risk_per_trade_alt": 0.75,
        "min_rr_sniper": 1.20,
        "min_rr_alt": 1.05,
        "require_grade_a_for_alt": False,
    },
    "AGGRESSIVE": {
        "max_portfolio_risk_pct": 5.0,
        "risk_per_trade_core": 1.25,
        "risk_per_trade_alt": 1.00,
        "min_rr_sniper": 1.10,
        "min_rr_alt": 1.00,
        "require_grade_a_for_alt": False,
    },
}


def calc_breakeven_price(avg_p: float, fee_pct: float = FEE_SPOT_PCT) -> float:
    fee_rate = fee_pct / 100.0
    return round(avg_p * ((1.0 + fee_rate) / (1.0 - fee_rate)), 8)


def get_trade_policy(mode: str) -> dict:
    return TRADE_POLICIES.get(mode, TRADE_POLICIES["NORMAL"])


def evaluate_setup_grade(cand: dict) -> str:
    mode = cand.get("entry_mode", "sniper")
    patterns = cand.get("patterns", [])
    sup_touches = cand.get("sup_touches", 0)
    vol_ratio = cand.get("signal_values", {}).get("volume", 1.0)
    rsi_val = cand.get("signal_values", {}).get("rsi", 50.0)
    tier = cand.get("tier", "SMALL")

    if mode == "reversal":
        has_pattern = ("pinbar" in patterns or "divergence" in patterns)
        has_level = (cand.get("nearest_sup") is not None and sup_touches >= 2)
        has_exhaustion = (rsi_val <= 36 or vol_ratio >= 1.5)
        if has_pattern and has_level and has_exhaustion:
            return "A+"
        return "B"

    elif mode == "rocket":
        is_high_cap = tier in ("TOP20", "MID")
        has_breakout = ("breakout" in patterns and vol_ratio >= 2.0)
        not_overheated = (rsi_val <= 75)
        if is_high_cap and has_breakout and not_overheated:
            return "A+"
        return "B"

    else:
        has_structure = (cand.get("nearest_sup") is not None and sup_touches >= 2)
        has_action = ("vcp" in patterns or "engulfing" in patterns or "pinbar" in patterns)
        if has_structure and has_action and vol_ratio >= 1.2:
            return "A+"
        return "A" if (has_structure or has_action) else "B"


def calc_total_open_risk_pct(positions: dict, orders: list, equity: float) -> float:
    if equity <= 0:
        return 0.0
    total_risk_usd = 0.0
    for sym, pos in positions.items():
        qty = pos.get("qty", 0.0)
        avg = pos.get("avg", 0.0)
        sl = pos.get("sl", 0.0)
        if avg > sl > 0:
            total_risk_usd += qty * (avg - sl)
    for o in orders:
        qty = o.get("qty", 0.0)
        price = o.get("price", 0.0)
        sl = o.get("sl", 0.0)
        if price > sl > 0:
            total_risk_usd += qty * (price - sl)
    return round((total_risk_usd / equity) * 100, 2)


def calc_initial_sl_tp(symbol: str, entry: float, tier: str, a: float, 
                        sup: float, sup_touches: int, res: float, res_touches: int,
                        is_mom: bool = False, entry_mode: str = "sniper",
                        min_rr: float = 1.20):
    tier = tier or "SMALL"
    sl_floor_pct = TIER_SL_FLOOR.get(tier, 0.020)
    tp_floor_pct = TIER_TP_FLOOR.get(tier, 0.028)
    max_sl_allowed = TIER_MAX_SL.get(tier, 3.5)

    sl_tuning = shadow.sl_mult()
    tp_tuning = shadow.tp_mult()

    base_mult = 0.75 if is_mom else (2.0 if entry_mode == "reversal" else 1.2)
    min_sl_dist = max(base_mult * a * sl_tuning, entry * sl_floor_pct)
    max_sl_dist = entry * (max_sl_allowed / 100.0)

    # 1. Расчет защитного стоп-лосса
    sl_dist = 0.0
    if sup and sup_touches >= 2 and sup < entry:
        struct_sl = round(sup * 0.997, 8)
        candidate_dist = entry - struct_sl
        if candidate_dist > max_sl_dist:
            return None, None, False, f"Опора слишком далеко ({candidate_dist/entry*100:.2f}% > {max_sl_allowed}%)"
        sl_dist = max(candidate_dist, min_sl_dist)
    else:
        sl_dist = min_sl_dist

    sl_dist = min(sl_dist, max_sl_dist)
    sl = round(entry - sl_dist, 8)

    sl_dist_pct = (entry - sl) / entry * 100
    if sl >= entry or sl_dist_pct > max_sl_allowed or sl_dist_pct < (sl_floor_pct * 100 * 0.75):
        return None, None, False, f"Недопустимая дистанция SL ({sl_dist_pct:.2f}%)"

    # 2. Расчет тейк-профита (TP1)
    tp_dist_raw = max(2.0 * a * tp_tuning, sl_dist * min_rr, entry * tp_floor_pct)
    tp = round(entry + tp_dist_raw, 8)

    if res and res > entry:
        res_dist_pct = (res - entry) / entry * 100
        if res_dist_pct < 0.8:
            return None, None, False, f"Стена сопротивления в упор ({res_dist_pct:.2f}% < 0.8%)"

        res_cap_tp = round(res * 0.997, 8)
        if res_cap_tp < tp:
            capped_rr = (res_cap_tp - entry) / sl_dist
            if capped_rr >= min_rr:
                tp = res_cap_tp
            else:
                return None, None, False, f"Стена не дает забрать {min_rr}x RR (RR={capped_rr:.2f})"

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

    breakeven = calc_breakeven_price(avg_p, FEE_SPOT_PCT)
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
