import time

import httpx
from loguru import logger

from bot.exchange.market_data import market_data
from bot.strategy.indicators import ema, rsi, atr
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.news.cmc import (get_coin_name, get_sectors_for_pool,
                          get_ranks_for_pool, tier_of, TIER_EMOJI, fetch_missing_names)
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache, check_sentiment

SCAN_SUMMARY = {"text": "", "thr": 0, "ts": 0}
FILTERED_BY_NEWS = []

SAT_ATR_PCT = 1.2
SCORE_MAX = 10.0

_instruments_cache = {"data": None, "ts": 0}

STABLE_BASES = {"USDC", "USDE", "DAI", "TUSD", "BUSD", "FDUSD", "USDP",
                "USD1", "USDD", "EUR", "EURT", "AEUR", "USDT",
                "RLUSD", "PYUSD", "EURI", "USDS", "USD0", "FRAX",
                "LUSD", "GUSD", "XUSD", "USDX", "CUSD", "SUSD"}


def is_tradable(symbol):
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in STABLE_BASES:
        return False
    if base.endswith(("3L", "3S", "5L", "5S")):
        return False
    return True


def raw_max_score(regime):
    """Теоретический максимум «сырого» скора при ТЕКУЩИХ весах (без режимного бонуса)."""
    m = sum(learner.weight(k) for k in
            ("ema50", "ema21", "impulse", "rsi", "volume", "chg24h"))
    m += learner.weight("indep")
    m += max(learner.weight("news_pos"), learner.weight("hype"))
    m += 1.0   # потолок секторного бонуса
    m += 0.5   # потолок тир-бонуса
    return m


def threshold(regime):
    """Порог на независимой 10-балльной шкале; строгость рынка — только здесь."""
    base = {"bull": 5.0, "neutral": 6.0, "bear": 7.0}.get(regime, 6.0)
    thr = base + learner.threshold_adj + shadow.threshold_nudge()
    return round(max(min(thr, SCORE_MAX - 0.5), SCORE_MAX * 0.5), 2)


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
    # 1. Получаем макро-тренд по BTC
    candles = await market_data.get_kline("BTCUSDT", "60", 250)
    if len(candles) < 60:
        return "neutral", {}
    closes = [c["close"] for c in candles]
    e50, e200, last = ema(closes, 50)[-1], ema(closes, 200)[-1], closes[-1]
    
    if last > e50 > e200:
        regime = "bull"
    elif last < e50 < e200:
        regime = "bear"
    else:
        regime = "neutral"

    # 2. Оцениваем "температуру альтов" (Market Breadth), если биток спит
    if regime == "neutral":
        tickers = await market_data.get_tickers()
        if tickers:
            tradable = [s for s, t in tickers.items() if is_tradable(s) and t["quote_volume"] >= 500_000]
            if tradable:
                # Считаем процент альтов, которые выросли более чем на 3% за 24 часа
                green_alts = sum(1 for s in tradable if tickers[s]["change_pct"] > 3.0)
                breadth_pct = (green_alts / len(tradable)) * 100
                
                # Если больше 25% ликвидных альтов уверенно растут - это альтсезон (локальный)
                if breadth_pct >= 25.0:
                    regime = "bull"
                    logger.info(f"Market Breadth: {breadth_pct:.1f}% альтов зеленые. Режим принудительно переведен в 'bull' (Альтсезон).")
                else:
                    logger.info(f"Market Breadth: {breadth_pct:.1f}% альтов зеленые. Режим остается 'neutral'.")

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
                        logger.error(f"Bybit API returned status {r.status_code}")
                        break
                    try:
                        data = r.json()
                    except Exception as e:
                        logger.error(f"Bybit API non-JSON response: {e}")
                        break
                    result = data.get("result", {})
                    items = result.get("list", [])
                    if not items:
                        break
                    raw += items
                    cursor = result.get("nextPageCursor", "") or ""
                    if not cursor:
                        break
            _instruments_cache["data"], _instruments_cache["ts"] = raw, now
            logger.info(f"Instruments loaded: {len(raw)} spot symbols")
        except Exception as e:
            logger.error(f"Bybit instruments error: {e}")
            raw = []

    now_ms = int(now * 1000)
    out = []
    for ins in raw:
        sym = ins.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if ins.get("status", "") != "Trading":
            continue
        try:
            launch = int(ins.get("launchTime", 0) or 0)
        except Exception:
            launch = 0
        if launch <= 0:
            continue
        out.append((sym, (now_ms - launch) / 3600000))
    return out
def score_symbol(candles, t, regime):
    closes = [c["close"] for c in candles]
    last = closes[-1]
    e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
    e12, e26 = ema(closes, 12)[-1], ema(closes, 26)[-1]
    r = rsi(closes)
    vols = [c["volume"] for c in candles]
    base_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else (sum(vols) / max(1, len(vols)))
    vol_ratio = vols[-1] / base_vol if base_vol else 1.0

    w = shadow.signal_windows()
    score, reasons, keys = 0.0, [], []
    if last > e50: score += learner.weight("ema50"); reasons.append("цена выше EMA50"); keys.append("ema50")
    if e21 > e50: score += learner.weight("ema21"); reasons.append("EMA21>EMA50"); keys.append("ema21")
    if e12 > e26: score += learner.weight("impulse"); reasons.append("импульс роста"); keys.append("impulse")
    if 40 <= r <= w["rsi_hi"]: 
        score += learner.weight("rsi"); reasons.append(f"RSI {r:.0f}"); keys.append("rsi")
    elif r > w["rsi_hi"]: 
        score -= 1.5; reasons.append(f"перегрев RSI {r:.0f}") # Штраф за покупку на хаях

    if vol_ratio > w["vol_lo"]: score += learner.weight("volume"); reasons.append(f"объём x{vol_ratio:.1f}"); keys.append("volume")
    
    if 0 < t["change_pct"] < w["chg_hi"]: 
        score += learner.weight("chg24h"); reasons.append(f"24ч +{t['change_pct']:.1f}%"); keys.append("chg24h")
    elif t["change_pct"] >= w["chg_hi"]: 
        score -= 1.0; reasons.append(f"памп +{t['change_pct']:.1f}% (уже поздно)") # Штраф за улетевший поезд
    if t["quote_volume"] < 500_000: score -= 1

    signal_values = {"rsi": r, "chg24h": t["change_pct"], "volume": vol_ratio}
    return score, reasons, keys, signal_values


def normalize(raw, regime):
    """Сырой скор → 10-балльная шкала (как в scan)."""
    rm = raw_max_score(regime)
    if rm <= 0:
        return 0.0
    return round(max(0.0, min(SCORE_MAX, raw / rm * SCORE_MAX)), 2)


async def live_score(sym, t, regime, news_items=None):
    """Оценка ТОЙ ЖЕ линейкой, что и scan: свечи 120, indep, сектор/тир,
    новости/хайп, нормализация."""
    candles = await market_data.get_kline(sym, "15", 120)
    if len(candles) < 60:
        return None, candles
    raw, _, _, _ = score_symbol(candles, t, regime)

    btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
    btc_ret = _returns([c["close"] for c in btc_candles])
    corr = _corr(_returns([c["close"] for c in candles]), btc_ret)
    if corr > 0.85 and regime == "neutral":
        raw -= 1
    elif corr < 0.45:
        raw += learner.weight("indep")

    base = sym[:-4]
    sectors_map = await get_sectors_for_pool([base])
    ranks_map = await get_ranks_for_pool([base])
    sb = learner.sector_bias(sectors_map.get(base, "Other"))
    if sb:
        raw += sb
    tb = learner.tier_bias(tier_of(ranks_map.get(base)))
    if tb:
        raw += tb

    if news_items is not None:
        name = await get_coin_name(base)
        neg, pos, mentions, _ = check_sentiment(news_items, [base, name])
        if pos > neg:
            raw += learner.weight("news_pos")
        elif mentions >= 2:
            raw += learner.weight("hype")

    score10 = normalize(raw, regime)
    
    # Применяем бонус к живому скору (только для ракет)
    _, _, keys_live, sv_live = score_symbol(candles, t, regime)
    is_mom_live = (sv_live.get("rsi", 0) >= 65 and sv_live.get("volume", 0) >= 1.5 and "impulse" in keys_live)
    entry_mode_live = "rocket" if is_mom_live else "sniper"
    mode_score_bonus, _ = learner.entry_mode_bias(entry_mode_live)
    
    if mode_score_bonus != 0.0:
        score10 = round(max(0.0, min(SCORE_MAX, score10 + mode_score_bonus)), 2)
        
    return score10, candles
async def scan(regime, tickers, limit=20):  # УВЕЛИЧИЛИ ЛИМИТ ОЧЕРЕДИ
    tradable = [s for s, t in tickers.items()
                if is_tradable(s) and t["quote_volume"] >= 200_000 and t["last"] > 0]

    by_vol = sorted(tradable, key=lambda s: tickers[s]["quote_volume"], reverse=True)[:40]
    by_chg = sorted([s for s in tradable if 0 < tickers[s]["change_pct"] < 25],
                    key=lambda s: tickers[s]["change_pct"], reverse=True)[:20]

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
        if sym in seen or not (24 <= age_h <= 336):
            continue
        seen.add(sym)
        if sym in tickers and is_tradable(sym) and tickers[sym]["quote_volume"] >= 500_000:
            by_listings.append((sym, age_h))
            logger.info(f"NEW LISTING: {sym} ({age_h:.1f}h old)")
    by_listings = by_listings[:10]

    pool = list(dict.fromkeys(by_vol + by_chg + by_momentum + by_volatility +
                              [s for s, _ in by_listings]))
    pool_bases = list({s[:-4] for s in pool})

    sectors_map = await get_sectors_for_pool(pool_bases)
    ranks_map = await get_ranks_for_pool(pool_bases)
    await fetch_missing_names(pool_bases)  # <--- МАССОВО ГРУЗИМ ИМЕНА БЕЗ ОШИБОК CMC

    news_items = await fetch_news_cache()
    btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
    btc_ret = _returns([c["close"] for c in btc_candles])

    raw_max = raw_max_score(regime)

    scored = []
    for sym in pool:
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        last_price = tickers[sym]["last"]
        atr_pct = (a / last_price) * 100 if last_price else 0
        if a <= 0 or atr_pct < 0.25:
            continue
        score, reasons, keys, signal_values = score_symbol(candles, tickers[sym], regime)

        corr = _corr(_returns([c["close"] for c in candles]), btc_ret)
        if corr > 0.85 and regime == "neutral":
            score -= 1
            reasons.append(f"зеркало BTC (corr {corr:.2f})")
        elif corr < 0.45:
            score += learner.weight("indep")
            reasons.append(f"независима от BTC (corr {corr:.2f})")
            keys.append("indep")

        kind = "satellite" if atr_pct >= SAT_ATR_PCT else "core"
        base = sym[:-4]
        sector = sectors_map.get(base, "Other")
        tier = tier_of(ranks_map.get(base))

        sb = learner.sector_bias(sector)
        if sb:
            score += sb
            reasons.append(f"сектор {sector}: {sb:+.2f}")
        tb = learner.tier_bias(tier)
        if tb:
            score += tb
            reasons.append(f"тир {TIER_EMOJI[tier]}: {tb:+.2f}")

        name = await get_coin_name(base)
        neg, pos, mentions, _ = check_sentiment(news_items, [base, name])
        
        # Токсичный фон: негатива больше позитива И он составляет не менее 20% от всех новостей по монете
        is_toxic = neg > 0 and neg > pos and neg >= (mentions * 0.2)
        
        if is_toxic:
            logger.info(f"{sym}: пропущен из-за негативного новостного фона ({neg} нег. из {mentions} упом.)")
            # Передаем строку формата "2/10", чтобы красиво выводилось по команде /news
            FILTERED_BY_NEWS.append({"symbol": sym, "neg_count": f"{neg}/{mentions}", "time": int(time.time())})
            FILTERED_BY_NEWS[:] = FILTERED_BY_NEWS[-10:]
            continue
        if pos > neg:
            score += learner.weight("news_pos")
            reasons.append(f"позитивный новостной фон ({pos})")
            keys.append("news_pos")
        elif mentions >= 2:
            score += learner.weight("hype")
            reasons.append(f"медиа-хайп ({mentions} упом.)")
            keys.append("hype")

        rm = raw_max
        score10 = (score / rm * SCORE_MAX) if rm > 0 else 0.0
        score10 = round(max(0.0, min(SCORE_MAX, score10)), 2)
        
        # Определяем "Ракету" (Моментум)
        sv = signal_values
        is_momentum = (sv.get("rsi", 0) >= 65 and sv.get("volume", 0) >= 1.5 and "impulse" in keys)
        
        # Умный сайзинг и бонус скора (только для ракет)
        entry_mode = "rocket" if is_momentum else "sniper"
        mode_score_bonus, mode_size_mult = learner.entry_mode_bias(entry_mode)
        
        if mode_score_bonus != 0.0:
            score10 = round(max(0.0, min(SCORE_MAX, score10 + mode_score_bonus)), 2)
            reasons.append(f"стат. входов (🚀): {mode_score_bonus:+.1f}")

        scored.append({"symbol": sym, "score": score10, "reasons": reasons,
                       "reason_keys": keys, "atr": a, "last": last_price,
                       "liquidity": tickers[sym]["quote_volume"],
                       "corr": round(corr, 2), "atr_pct": round(atr_pct, 2),
                       "kind": kind, "sector": sector, "tier": tier,
                       "signal_values": signal_values,
                       "is_momentum": is_momentum,
                       "size_mult": mode_size_mult})

    scored.sort(key=lambda c: c["score"], reverse=True)

    thr = threshold(regime)
    try:
        shadow.observe(scored, regime, thr)
        shadow.tick(tickers)
        shadow.autotune()
    except Exception as e:
        logger.error(f"shadow error: {e}")

    parts_html, parts_plain = [], []
    for c in scored[:5]:
        k_tag = "🛰" if c.get("kind") == "satellite" else "🏛"
        parts_html.append(
            f"{k_tag} <b>{c['symbol'][:-4]}</b> {TIER_EMOJI.get(c['tier'], '🐭')} · "
            f"<i>{c['sector']}</i> · {c['score']:.1f}/{thr:g} · ₿ {c['corr']:.2f}"
        )
        keys_short = "+".join(c["reason_keys"][:5])
        parts_plain.append(f"{c['symbol']} {c['score']:.1f}/{thr:g} [{keys_short}] btc{c['corr']:.2f}")
    SCAN_SUMMARY["text"] = " | ".join(parts_html) or "сигналов нет"
    SCAN_SUMMARY["thr"] = thr
    SCAN_SUMMARY["ts"] = time.time()
    logger.info(f"Scan top: {' | '.join(parts_plain) or 'сигналов нет'}")

    new_set = set(s for s, _ in by_listings)
    candidates = []
    for c in scored:
        if c["score"] < thr:
            continue
        c["is_new"] = c["symbol"] in new_set
        candidates.append(c)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]
