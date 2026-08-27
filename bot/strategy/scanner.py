import time

import httpx
from loguru import logger

from bot.exchange.market_data import market_data
from bot.strategy.indicators import ema, rsi, atr
from bot.strategy.learner import learner
from bot.news.cmc import get_coin_name, get_sectors_for_pool
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache, check_sentiment

SCAN_SUMMARY = {"text": "", "thr": 0, "ts": 0}
FILTERED_BY_NEWS = []

SAT_ATR_PCT = 1.2  # порог волатильности: выше — монета идёт в сателлиты

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


def threshold(regime):
    base = {"bull": 5.0, "neutral": 6.0, "bear": 8.0}.get(regime, 6.0)
    return min(9.5, max(5.0, base + learner.threshold_adj))


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
    return regime, {"btc": last, "ema50": e50, "ema200": e200}


async def fetch_new_listings():
    """Новые листинги через Bybit API (launchTime) — надёжный источник. Кэш 1 час."""
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

    score, reasons, keys = 0.0, [], []
    if last > e50: score += learner.weight("ema50"); reasons.append("цена выше EMA50"); keys.append("ema50")
    if e21 > e50: score += learner.weight("ema21"); reasons.append("EMA21>EMA50"); keys.append("ema21")
    if e12 > e26: score += learner.weight("impulse"); reasons.append("импульс роста"); keys.append("impulse")
    if 40 <= r <= 65: score += learner.weight("rsi"); reasons.append(f"RSI {r:.0f}"); keys.append("rsi")
    if vol_ratio > 1.3: score += learner.weight("volume"); reasons.append(f"объём x{vol_ratio:.1f}"); keys.append("volume")
    if 0 < t["change_pct"] < 12: score += learner.weight("chg24h"); reasons.append(f"24ч +{t['change_pct']:.1f}%"); keys.append("chg24h")
    if regime == "bull": score += 1
    if regime == "bear": score -= 2
    if t["quote_volume"] < 500_000: score -= 1
    return score, reasons, keys


async def scan(regime, tickers, limit=5):
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

    # АВТО-СЕКТОРА: ручной словарь -> кэш -> теги CMC
    sectors_map = await get_sectors_for_pool(list({s[:-4] for s in pool}))

    news_items = await fetch_news_cache()
    btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
    btc_ret = _returns([c["close"] for c in btc_candles])

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
        score, reasons, keys = score_symbol(candles, tickers[sym], regime)

        corr = _corr(_returns([c["close"] for c in candles]), btc_ret)
        if corr > 0.85 and regime == "neutral":
            score -= 1
            reasons.append(f"зеркало BTC (corr {corr:.2f})")
        elif corr < 0.45:
            score += learner.weight("indep")
            reasons.append(f"независима от BTC (corr {corr:.2f})")
            keys.append("indep")

        kind = "satellite" if atr_pct >= SAT_ATR_PCT else "core"
        sector = sectors_map.get(sym[:-4], "Other")

        # СЕКТОРНОЕ САМООБУЧЕНИЕ: бонус/штраф по истории сектора
        bias = learner.sector_bias(sector)
        if bias:
            score += bias
            reasons.append(f"сектор {sector}: {bias:+.2f}")

        scored.append({"symbol": sym, "score": score, "reasons": reasons,
                       "reason_keys": keys, "atr": a, "last": last_price,
                       "liquidity": tickers[sym]["quote_volume"],
                       "corr": round(corr, 2), "atr_pct": round(atr_pct, 2),
                       "kind": kind, "sector": sector})

    scored.sort(key=lambda c: c["score"], reverse=True)

    thr = threshold(regime)
    # Топ-5 в единой стилистике (HTML для Telegram) + plain для логов
    parts_html = []
    parts_plain = []
    for c in scored[:5]:
        k_tag = "🛰" if c.get("kind") == "satellite" else "🏛"
        parts_html.append(
            f"{k_tag} <b>{c['symbol'][:-4]}</b> · <i>{c['sector']}</i> · {c['score']:.1f}/{thr:g}"
        )
        parts_plain.append(f"{c['symbol']} {c['score']:.1f}/{thr:g}")
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

        base = c["symbol"][:-4]
        name = await get_coin_name(base)
        neg, pos, mentions, heads = check_sentiment(news_items, [base, name])
        if neg > 0 and neg > pos:
            logger.info(f"{c['symbol']}: пропущен из-за негативного новостного фона ({neg})")
            FILTERED_BY_NEWS.append({
                "symbol": c['symbol'],
                "neg_count": neg,
                "time": int(time.time()),
            })
            FILTERED_BY_NEWS[:] = FILTERED_BY_NEWS[-10:]
            continue
        if pos > neg:
            c["score"] += learner.weight("news_pos")
            c["reason_keys"].append("news_pos")
            c["reasons"].append(f"позитивный новостной фон ({pos})")
        elif mentions >= 2:
            c["score"] += learner.weight("hype")
            c["reason_keys"].append("hype")
            c["reasons"].append(f"медиа-хайп ({mentions} упом.)")
        candidates.append(c)

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]
