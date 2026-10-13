import os
import time
import asyncio

import httpx
from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.indicators import ema, rsi, atr, find_sr_levels, get_all_sr_levels
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.strategy.patterns import detect_patterns
from bot.news.cmc import (get_coin_name, get_sectors_for_pool,
                          get_ranks_for_pool, tier_of, TIER_EMOJI, fetch_missing_names)
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache, check_sentiment
from bot.strategy.fundamental import get_macro_trend, is_sector_hot, get_fear_and_greed, check_coinglass_liquidation_threat
from bot.utils.format import fmt_price

SCAN_SUMMARY = {"text": "", "thrs": {}, "ts": 0}
FILTERED_BY_NEWS = []

SCORE_MAX = 10.0

_instruments_cache = {"data": None, "ts": 0}

STABLE_BASES = {"USDC", "USDE", "DAI", "TUSD", "BUSD", "FDUSD", "USDP",
                "USD1", "USDD", "EUR", "EURT", "AEUR", "USDT",
                "RLUSD", "PYUSD", "EURI", "USDS", "USD0", "FRAX",
                "LUSD", "GUSD", "XUSD", "USDX", "CUSD", "SUSD"}

BASE_THRESHOLDS = {
    "bull": {"rocket": 6.0, "sniper": 5.5, "reversal": 5.5},
    "neutral": {"rocket": 6.5, "sniper": 6.0, "reversal": 5.4},
    "bear": {"rocket": 7.0, "sniper": 6.5, "reversal": 5.2},
}

TIER_CORR_LIMITS = {
    "TOP20": 0.92,
    "MID":   0.88,
    "SMALL": 0.82,
    "MICRO": 0.75,
}

_regime_tracker = {
    "prev_regime": "neutral",
    "last_bear_ts": 0.0,
    "bull_start_ts": 0.0,
    "breadth_streak": 0,
}

def _init_regime_tracker():
    if _regime_tracker["last_bear_ts"] == 0.0 and hasattr(paper, "market_history") and paper.market_history:
        for h in reversed(paper.market_history):
            if h.get("regime") == "bear" and _regime_tracker["last_bear_ts"] == 0.0:
                _regime_tracker["last_bear_ts"] = float(h.get("ts", 0))
            if h.get("regime") == "bull" and _regime_tracker["bull_start_ts"] == 0.0:
                _regime_tracker["bull_start_ts"] = float(h.get("ts", 0))
        _regime_tracker["prev_regime"] = paper.market_history[-1].get("regime", "neutral")


def get_thresholds(regime):
    now = time.time()
    bull_start = _regime_tracker.get("bull_start_ts", 0.0)
    bull_age = (now - bull_start) if bull_start > 0 else 0

    if regime == "bull" and (bull_start == 0.0 or bull_age < 1800):
        base = BASE_THRESHOLDS["neutral"]
    else:
        base = BASE_THRESHOLDS.get(regime, BASE_THRESHOLDS["neutral"])

    adj = learner.threshold_adj 
    t = shadow.tuning

    return {
        "rocket": round(max(0.0, min(SCORE_MAX - 0.5, base["rocket"] + adj + t.get("nudge_rocket", 0.0))), 2),
        "sniper": round(max(0.0, min(SCORE_MAX - 0.5, base["sniper"] + adj + t.get("nudge_sniper", 0.0))), 2),
        "reversal": round(max(0.0, min(SCORE_MAX - 0.5, base["reversal"] + adj + t.get("nudge_reversal", 0.0))), 2),
    }


def is_tradable(symbol):
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in STABLE_BASES:
        return False
    if base.endswith(("3L", "3S", "5L", "5S")):
        return False
    return True


def is_post_pump_dump(candles_15m: list) -> tuple:
    if not candles_15m or len(candles_15m) < 16:
        return False, ""
    recent = candles_15m[-16:]
    
    high_val = -1.0
    high_idx = -1
    low_val = float("inf")
    low_idx = -1

    for idx, c in enumerate(recent):
        if c["high"] > high_val:
            high_val = c["high"]
            high_idx = idx
        if c["low"] < low_val:
            low_val = c["low"]
            low_idx = idx

    if low_idx < high_idx and low_val > 0:
        pump_magnitude = (high_val - low_val) / low_val * 100
        if pump_magnitude >= 12.0 and high_idx <= len(recent) - 2:
            last_close = recent[-1]["close"]
            dump_depth = (high_val - last_close) / (high_val - low_val)
            if dump_depth >= 0.40:
                return True, f"пост-памп дамп (+{pump_magnitude:.1f}%, слив {dump_depth*100:.0f}%)"

    return False, ""


def raw_max_score(regime):
    m = sum(learner.weight(k) for k in ("ema50", "ema21", "impulse", "rsi", "volume", "chg24h"))
    m += learner.weight("indep")
    m += learner.weight("news_pos")
    m += learner.weight("hype")
    m += 3.0
    m += 1.5
    m += 0.5
    return m


def _returns(closes):
    return [(b - a) / a for a, b in zip(closes, closes[1:]) if a]


def _corr(a, b):
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / (va * vb) ** 0.5


async def get_regime():
    now = time.time()
    _init_regime_tracker()

    candles = await market_data.get_kline("BTCUSDT", "60", 250)
    if len(candles) < 60:
        return _regime_tracker.get("prev_regime", "neutral"), {}
    closes = [c["close"] for c in candles]
    e21, e50, e200 = ema(closes, 21)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    last = closes[-1]

    if e21 > e50 > e200:
        raw_regime = "bull"
    elif e21 < e50 < e200:
        raw_regime = "bear"
    else:
        raw_regime = "neutral"

    tickers = await market_data.get_tickers()
    if tickers:
        tradable = [s for s, t in tickers.items() if is_tradable(s) and t["quote_volume"] >= 500_000]
        if tradable:
            green_alts = sum(1 for s in tradable if tickers[s]["change_pct"] > 3.0)
            breadth_pct = (green_alts / len(tradable)) * 100

            if breadth_pct >= 45.0:
                _regime_tracker["breadth_streak"] += 1
                if _regime_tracker["breadth_streak"] >= 2 and raw_regime == "neutral":
                    raw_regime = "bull"
                    logger.info(f"Market Breadth: {breadth_pct:.1f}% альтов зеленые (подтверждено 2-й цикл). Переход в 'bull'.")
                elif _regime_tracker["breadth_streak"] == 1 and raw_regime == "neutral":
                    logger.info(f"Market Breadth: {breadth_pct:.1f}% альтов зеленые (цикл 1/2). Ожидание подтверждения для BULL.")
            else:
                _regime_tracker["breadth_streak"] = 0

    stable_trend = get_macro_trend()
    fng = get_fear_and_greed()

    if stable_trend == "bull" and raw_regime == "neutral":
        raw_regime = "bull"
    elif stable_trend == "bear" and raw_regime == "bull":
        raw_regime = "neutral" 

    if fng < 40 and raw_regime == "bull":
        raw_regime = "neutral" 
    elif fng > 75 and raw_regime == "bear":
        raw_regime = "neutral" 

    regime = raw_regime

    if regime == "bear":
        _regime_tracker["last_bear_ts"] = now
        _regime_tracker["bull_start_ts"] = 0.0
    elif regime == "bull":
        time_since_bear = now - _regime_tracker["last_bear_ts"]
        if _regime_tracker["last_bear_ts"] > 0 and time_since_bear < 2700:
            rem_min = (2700 - time_since_bear) / 60
            regime = "neutral"
            logger.info(f"🛡 Гистерезис: выход из BEAR заблокировал прямой прыжок в BULL. Буфер NEUTRAL еще {rem_min:.1f} мин.")
        else:
            if _regime_tracker["bull_start_ts"] == 0.0:
                _regime_tracker["bull_start_ts"] = now
                logger.info("🚀 Подтвержден старт фазы BULL. Запущен 30-минутный прогрев порогов.")
    else:
        _regime_tracker["bull_start_ts"] = 0.0

    _regime_tracker["prev_regime"] = regime
    return regime, {"btc": last, "ema50": e50, "ema200": e200}


async def fetch_new_listings():
    now = time.time()
    if _instruments_cache["data"] is not None and now - _instruments_cache["ts"] < 3600:
        raw = _instruments_cache["data"]
    else:
        raw = []
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                cursor = ""
                for page in range(5):
                    params = {"category": "spot", "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    r = await c.get("https://api.bybit.com/v5/market/instruments-info", params=params)
                    if r.status_code != 200:
                        break
                    data = r.json()
                    result = data.get("result", {})
                    items = result.get("list", [])
                    if not items:
                        break
                    raw += items
                    cursor = result.get("nextPageCursor", "") or ""
                    if not cursor:
                        break
            _instruments_cache["data"], _instruments_cache["ts"] = raw, now
        except Exception:
            raw = []

    now_ms = int(now * 1000)
    out = []
    for ins in raw:
        sym = ins.get("symbol", "")
        if not sym.endswith("USDT") or ins.get("status", "") != "Trading":
            continue
        try:
            launch = int(ins.get("launchTime", 0) or 0)
        except Exception:
            launch = 0
        if launch > 0:
            out.append((sym, (now_ms - launch) / 3600000))
    return out


def score_symbol(candles_15m, candles_1h, t, regime, is_open_pos=False):
    closes = [c["close"] for c in candles_15m]
    last = closes[-1]
    e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
    e12, e26 = ema(closes, 12)[-1], ema(closes, 26)[-1]
    r = rsi(closes)
    a15 = atr(candles_15m)

    vols = [c["volume"] for c in candles_15m]
    base_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else (sum(vols) / max(1, len(vols)))
    vol_ratio = vols[-1] / base_vol if base_vol else 1.0

    prior_candles = candles_15m[-25:-1] if len(candles_15m) >= 25 else candles_15m[:-1]
    recent_high = max(c["high"] for c in prior_candles) if prior_candles else last
    recent_low_2h = min(c["low"] for c in candles_15m[-8:]) if len(candles_15m) >= 8 else min(c["low"] for c in candles_15m)

    w = shadow.signal_windows()
    score, reasons, keys = 0.0, [], []

    # 1. Трендовый контекст
    if last > e50: score += learner.weight("ema50"); reasons.append("цена выше EMA50"); keys.append("ema50")
    if e21 > e50: score += learner.weight("ema21"); reasons.append("EMA21>EMA50"); keys.append("ema21")
    if e12 > e26: score += learner.weight("impulse"); reasons.append("импульс роста"); keys.append("impulse")

    # 2. RSI
    if r < 38:
        score += learner.weight("rsi"); reasons.append(f"перепроданность RSI {r:.0f}"); keys.append("rsi")
    elif 38 <= r <= w["rsi_hi"]:
        score += learner.weight("rsi"); reasons.append(f"RSI {r:.0f}"); keys.append("rsi")
    elif r > w["rsi_hi"]: 
        score -= 1.5; reasons.append(f"перегрев RSI {r:.0f}") 

    # 3. Объем
    last_candle = candles_15m[-1]
    is_green = last_candle["close"] >= last_candle["open"]
    c_range = last_candle["high"] - last_candle["low"]
    lower_wick = min(last_candle["open"], last_candle["close"]) - last_candle["low"]
    is_panic_dip = (t["change_pct"] <= -6.0 or r < 38)

    if vol_ratio > w["vol_lo"]:
        has_bottom_wick = c_range > 0 and (lower_wick / c_range) >= 0.40
        if is_green or has_bottom_wick:
            score += learner.weight("volume")
            reasons.append(f"объём покупок x{vol_ratio:.1f}")
            keys.append("volume")
        elif not is_panic_dip:
            score -= 2.0
            reasons.append(f"сливной объём x{vol_ratio:.1f} (давление продаж)")

    recent_rejection = False
    for c in candles_15m[-3:]:
        c_rng = c["high"] - c["low"]
        c_w = c["high"] - max(c["close"], c["open"])
        if c_rng > 0 and (c_w / c_rng) > 0.45:
            recent_rejection = True
            break
    if recent_rejection and not is_open_pos:
        score -= 3.0
        reasons.append("отвержение на хаях (шпилька продаж)")

    if not is_open_pos and a15 > 0 and e21 > 0:
        dist_from_ema = last - e21
        if dist_from_ema > 2.0 * a15:
            score -= 4.0
            reasons.append(f"FOMO-перегрев (+{dist_from_ema/a15:.1f} ATR от EMA21)")

    # 4. Триггеры Price Action
    detected_patterns = detect_patterns(candles_15m, a15, e50, e21, recent_high)
    matched_modes = []
    pattern_keys = []

    for pat in detected_patterns:
        pkey = pat["key"]
        p_bonus = pat["base_bonus"] * learner.pattern_multiplier(pkey)
        score += p_bonus
        reasons.append(f"{pat['name']} (+{p_bonus:.1f})")
        pattern_keys.append(pkey)
        matched_modes.append(pat["mode"])

    # 5. Динамика 24h
    if 0 < t["change_pct"] < w["chg_hi"]:
        score += learner.weight("chg24h")
        reasons.append(f"24ч +{t['change_pct']:.1f}%")
        keys.append("chg24h")
    elif t["change_pct"] <= -5.0 and ("reversal" in matched_modes or r < 38):
        score += learner.weight("chg24h")
        reasons.append(f"дисконт 24ч {t['change_pct']:.1f}%")
        keys.append("chg24h")
    elif t["change_pct"] >= w["chg_hi"]:
        score -= 1.0
        reasons.append(f"памп +{t['change_pct']:.1f}% (уже поздно)")

    if t["quote_volume"] < 500_000:
        score -= 1.0

    # 6. S/R уровни
    nearest_res, res_touches, nearest_sup, sup_touches = find_sr_levels(candles_15m, candles_1h)
    
    if nearest_res and res_touches >= 2 and a15 > 0 and not is_open_pos:
        dist_to_res = nearest_res - last
        if dist_to_res < 0.8 * a15 or (dist_to_res / last * 100) < 0.7:
            score -= 4.0
            reasons.append(f"в упор к стене ${fmt_price(nearest_res)} ({res_touches}x)")

    if nearest_sup and sup_touches >= 2 and a15 > 0 and not is_open_pos:
        dist_to_sup = last - nearest_sup
        if dist_to_sup < 1.2 * a15 and last >= nearest_sup and last > closes[-2]:
            score += 1.5
            reasons.append(f"отскок от поддержки ${fmt_price(nearest_sup)} ({sup_touches}x)")
            keys.append("support_bounce")

    if candles_1h and len(candles_1h) >= 25:
        closes_1h = [c["close"] for c in candles_1h]
        last_1h = closes_1h[-1]
        e21_1h = ema(closes_1h, 21)[-1]
        e50_1h = ema(closes_1h, 50)[-1]
        e_trend_1h = ema(closes_1h, 200)[-1] if len(closes_1h) >= 200 else e50_1h
        r_1h = rsi(closes_1h)

        global_uptrend = last_1h > e_trend_1h
        local_dip = r_1h < 45 
        micro_turn = e12 > e26 

        if global_uptrend and local_dip and micro_turn:
            score += 1.5
            reasons.append("MTF: выкуп отката по тренду")
            keys.append("mtf_dip")
        elif not global_uptrend and r_1h > 65:
            score -= 1.0
            reasons.append("MTF: ловушка (даунтренд)")

        is_rev_candidate = ("pinbar" in pattern_keys or "divergence" in pattern_keys)
        if last_1h < e21_1h and not is_open_pos and not is_rev_candidate:
            score -= 3.5
            reasons.append(f"1H даунтренд (цена < EMA21_1H ${fmt_price(e21_1h)})")

    has_structure = (
        (nearest_sup is not None and sup_touches >= 2) or
        len(pattern_keys) > 0 or
        ("support_bounce" in keys) or
        ("mtf_dip" in keys)
    )

    confirmed_res, confirmed_sup = get_all_sr_levels(candles_15m, candles_1h)
    
    signal_values = {
        "rsi": r, "chg24h": t["change_pct"], "volume": vol_ratio,
        "nearest_res": nearest_res, "res_touches": res_touches,
        "nearest_sup": nearest_sup, "sup_touches": sup_touches,
        "all_res": confirmed_res,
        "all_sup": confirmed_sup,
        "recent_high": recent_high,
        "recent_low_2h": recent_low_2h,
        "matched_modes": matched_modes,
        "patterns": pattern_keys,
        "has_structure": has_structure
    }
    return score, reasons, keys, signal_values


def normalize(raw, regime):
    rm = raw_max_score(regime)
    if rm <= 0: return 0.0
    return round(max(0.0, min(SCORE_MAX, raw / rm * SCORE_MAX)), 2)


async def live_score(sym, t, regime, btc_ret, news_items=None, deriv_t=None, is_open_pos=False):
    candles_15m = await market_data.get_kline(sym, "15", 120)
    candles_1h = await market_data.get_kline(sym, "60", 250)

    if len(candles_15m) < 60 or len(candles_1h) < 60:
        return None, candles_15m

    raw, _, keys_live, sv_live = score_symbol(candles_15m, candles_1h, t, regime, is_open_pos)

    base = sym[:-4]
    sectors_map = await get_sectors_for_pool([base])
    ranks_map = await get_ranks_for_pool([base])
    sector = sectors_map.get(base, "Other")
    tier = tier_of(ranks_map.get(base))

    corr = _corr(_returns([c["close"] for c in candles_15m]), btc_ret)
    tier_corr_limit = TIER_CORR_LIMITS.get(tier, 0.82)

    if corr > tier_corr_limit and regime == "neutral":
        raw -= 1
    elif corr < 0.45:
        raw += learner.weight("indep")

    fng = get_fear_and_greed()
    if fng >= 80: raw -= 0.5
    elif fng <= 25: raw += 0.5

    if is_sector_hot(sector):
        raw += 0.5

    sb = learner.sector_bias(sector)
    if sb: raw += sb
    tb = learner.tier_bias(tier)
    if tb: raw += tb

    if news_items is not None:
        name = await get_coin_name(base)
        neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
        if pos_news > neg:
            raw += learner.weight("news_pos")
        elif mentions >= 2:
            raw += learner.weight("hype")

    score10 = normalize(raw, regime)

    modes = sv_live.get("matched_modes", [])
    if "reversal" in modes: entry_mode_live = "reversal"
    elif "rocket" in modes: entry_mode_live = "rocket"
    else: entry_mode_live = "sniper"

    if deriv_t:
        funding = deriv_t.get("funding", 0)
        if funding > 0.05 and entry_mode_live == "rocket":
            score10 -= 0.5
        elif funding < -0.01:
            score10 += 0.5

    is_sqz = await check_coinglass_liquidation_threat(sym)
    if is_sqz: score10 -= 1.5 

    score10 = round(max(0.0, min(SCORE_MAX, score10)), 2)    
    return score10, candles_15m


async def scan(regime, tickers, deriv_tickers, limit=20):
    tradable = [s for s, t in tickers.items() if is_tradable(s) and t["quote_volume"] >= 200_000 and t["last"] > 0]

    by_vol = sorted(tradable, key=lambda s: tickers[s]["quote_volume"], reverse=True)[:40]
    by_chg = sorted([s for s in tradable if 0 < tickers[s]["change_pct"] < 25], key=lambda s: tickers[s]["change_pct"], reverse=True)[:20]

    by_momentum = []
    for sym in tradable[:50]:
        candles = await market_data.get_kline(sym, "60", 168)
        if len(candles) >= 100:
            chg_7d = (candles[-1]["close"] - candles[0]["close"]) / candles[0]["close"] * 100
            if 5 < chg_7d < 50:
                by_momentum.append((sym, chg_7d))
    by_momentum = [s for s, _ in sorted(by_momentum, key=lambda x: x[1], reverse=True)][:15]

    by_volatility = []
    for sym in tradable[:50]:
        candles = await market_data.get_kline(sym, "15", 60)
        if len(candles) >= 30:
            a = atr(candles)
            last = candles[-1]["close"]
            atr_pct = (a / last) * 100 if last else 0
            if atr_pct > 2.5:
                by_volatility.append((sym, atr_pct))
    by_volatility = [s for s, _ in sorted(by_volatility, key=lambda x: x[1], reverse=True)][:10]

    by_dip = sorted([s for s in tradable if tickers[s]["change_pct"] <= -6.0], key=lambda s: tickers[s]["change_pct"])[:15]

    sources = await fetch_new_listings()
    try:
        rss = await fetch_listings_cache()
        now_ts = int(time.time())
        sources += [(l["symbol"], (now_ts - l["ts"]) / 3600) for l in rss]
    except Exception as e:
        logger.debug(f"RSS listings unavailable: {e}")

    by_listings = []
    seen = set()
    for sym, age_h in sorted(sources, key=lambda x: x[1]):
        if sym in seen or not (24 <= age_h <= 336): continue
        seen.add(sym)
        if sym in tickers and is_tradable(sym) and tickers[sym]["quote_volume"] >= 500_000:
            by_listings.append((sym, age_h))
    by_listings = by_listings[:10]

    pool = list(dict.fromkeys(by_vol + by_chg + by_momentum + by_volatility + by_dip + [s for s, _ in by_listings]))
    pool_bases = list({s[:-4] for s in pool})

    sectors_map = await get_sectors_for_pool(pool_bases)
    ranks_map = await get_ranks_for_pool(pool_bases)
    await fetch_missing_names(pool_bases)

    news_items = await fetch_news_cache()

    btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
    btc_ret = _returns([c["close"] for c in btc_candles])

    raw_max = raw_max_score(regime)
    fng = get_fear_and_greed()

    if not paper.market_history or paper.market_history[-1].get("regime") != regime:
        paper.market_history.append({
            "ts": int(time.time()),
            "regime": regime,
            "mode": getattr(learner, "current_risk_mode", "NORMAL")
        })
        paper.market_history = paper.market_history[-500:]
        paper.save()

    sem = asyncio.Semaphore(10)

    async def _analyze_sym(sym):
        async with sem:
            candles_15m = await market_data.get_kline(sym, "15", 120)
            candles_1h = await market_data.get_kline(sym, "60", 250)

            if len(candles_15m) < 60 or len(candles_1h) < 60: return None

            is_post_pump, pp_reason = is_post_pump_dump(candles_15m)
            if is_post_pump:
                return None

            a = atr(candles_15m)
            last_price = tickers[sym]["last"]
            atr_pct = (a / last_price) * 100 if last_price else 0
            if a <= 0 or atr_pct < 0.25: return None

            score, reasons, keys, signal_values = score_symbol(candles_15m, candles_1h, tickers[sym], regime)

            if not signal_values.get("has_structure"):
                return None

            base = sym[:-4]
            sector = sectors_map.get(base, "Other")
            tier = tier_of(ranks_map.get(base))

            corr = _corr(_returns([c["close"] for c in candles_15m]), btc_ret)
            tier_corr_limit = TIER_CORR_LIMITS.get(tier, 0.82)

            if corr > tier_corr_limit and regime == "neutral":
                score -= 1
                reasons.append(f"зеркало BTC ({tier}: corr {corr:.2f})")
            elif corr < 0.45:
                score += learner.weight("indep")
                reasons.append(f"независима от BTC (corr {corr:.2f})")
                keys.append("indep")

            if fng >= 80:  
                score -= 0.5
                reasons.append(f"F&G перегрев ({fng}): -0.5")
            elif fng <= 25: 
                score += 0.5
                reasons.append(f"F&G страх ({fng}): +0.5")

            sb = learner.sector_bias(sector)
            if sb:
                score += sb
                reasons.append(f"сектор {sector}: {sb:+.2f}")

            if is_sector_hot(sector):
                score += 0.5
                reasons.append(f"TVL приток (DefiLlama): +0.5")

            tb = learner.tier_bias(tier)
            if tb:
                score += tb
                reasons.append(f"тир {TIER_EMOJI[tier]}: {tb:+.2f}")

            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            is_toxic = neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)
            if is_toxic:
                FILTERED_BY_NEWS[:] = [item for item in FILTERED_BY_NEWS if item["symbol"] != sym]
                FILTERED_BY_NEWS.append({"symbol": sym, "neg_count": f"{neg}/{mentions}", "time": int(time.time())})
                FILTERED_BY_NEWS[:] = FILTERED_BY_NEWS[-10:]
                return None

            if pos_news > neg:
                score += learner.weight("news_pos")
                reasons.append(f"позитивный новостной фон ({pos_news})")
                keys.append("news_pos")
            elif mentions >= 2:
                score += learner.weight("hype")
                reasons.append(f"медиа-хайп ({mentions} упом.)")
                keys.append("hype")

            score10 = (score / raw_max * SCORE_MAX) if raw_max > 0 else 0.0

            modes = signal_values.get("matched_modes", [])
            if "reversal" in modes: entry_mode = "reversal"
            elif "rocket" in modes: entry_mode = "rocket"
            else: entry_mode = "sniper"

            deriv_t = deriv_tickers.get(sym)
            if deriv_t:
                funding = deriv_t.get("funding", 0)
                if funding > 0.05 and entry_mode == "rocket":
                    score10 -= 0.5
                    reasons.append(f"перегрев лонгов Фьюч ({-0.5})")
                elif funding < -0.01:
                    score10 += 0.5
                    reasons.append(f"шорт-сквиз потенциал Фьюч ({+0.5})")

            is_sqz = await check_coinglass_liquidation_threat(sym)
            if is_sqz:
                score10 -= 1.5
                reasons.append(f"риск ликвидаций Coinglass ({-1.5})")

            score10 = round(max(0.0, min(SCORE_MAX, score10)), 2)

            return {
                "symbol": sym, "score": score10, "reasons": reasons,
                "reason_keys": keys, "atr": a, "last": last_price,
                "liquidity": tickers[sym]["quote_volume"],
                "corr": round(corr, 2), "atr_pct": round(atr_pct, 2),
                "sector": sector, "tier": tier,
                "signal_values": signal_values,
                "patterns": signal_values.get("patterns", []),
                "is_momentum": (entry_mode == "rocket"),
                "entry_mode": entry_mode,
                "nearest_res": signal_values.get("nearest_res"),
                "res_touches": signal_values.get("res_touches", 0),
                "nearest_sup": signal_values.get("nearest_sup"),
                "sup_touches": signal_values.get("sup_touches", 0),
                "recent_high": signal_values.get("recent_high"),
                "recent_low_2h": signal_values.get("recent_low_2h"),
                "size_mult": 1.0
            }

    tasks = [_analyze_sym(sym) for sym in pool]
    results = await asyncio.gather(*tasks)

    scored = [r for r in results if r is not None]
    scored.sort(key=lambda c: c["score"], reverse=True)

    thrs = get_thresholds(regime)
    try:
        shadow.observe(scored, regime, thrs)
        shadow.tick(tickers)
        shadow.autotune()
    except Exception as e:
        logger.error(f"shadow error: {e}")

    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    parts_html, parts_plain = [], []
    for c in scored[:5]:
        chart_url = f"{public_url}/chart?symbol={c['symbol']}"
        mode = c.get("entry_mode", "sniper")
        thr = thrs.get(mode, 6.0)
        mode_icon = "🚀" if mode == "rocket" else "🧲" if mode == "reversal" else "🏹"

        parts_html.append(f"{mode_icon} <a href='{chart_url}'><b>{c['symbol'][:-4]}</b></a> {TIER_EMOJI.get(c['tier'], '🐭')} · <i>{c['sector']}</i> · {c['score']:.1f}/{thr:g} · ₿ {c['corr']:.2f}")
        parts_plain.append(f"{c['symbol']} {c['score']:.1f}/{thr:g} [{'+'.join(c['reason_keys'][:5])}] btc{c['corr']:.2f}")

    SCAN_SUMMARY["text"] = " | ".join(parts_html) or "сигналов нет"
    SCAN_SUMMARY["thrs"] = thrs
    SCAN_SUMMARY["ts"] = time.time()
    logger.info(f"Scan top: {' | '.join(parts_plain) or 'сигналов нет'}")

    new_set = set(s for s, _ in by_listings)
    candidates = []
    for c in scored:
        mode = c.get("entry_mode", "sniper")
        if c["score"] < thrs.get(mode, 6.0): continue
        c["is_new"] = c["symbol"] in new_set
        candidates.append(c)

    return candidates[:limit]
