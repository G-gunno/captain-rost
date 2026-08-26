import time

from loguru import logger

from bot.exchange.market_data import market_data
from bot.strategy.indicators import ema, rsi, atr
from bot.strategy.learner import learner
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment

SCAN_SUMMARY = {"text": "", "thr": 0, "ts": 0}
FILTERED_BY_NEWS = []  # последние монеты, отфильтрованные из-за негативных новостей

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
    base = {"bull": 5, "neutral": 6, "bear": 8}.get(regime, 6)
    return base + learner.threshold_adj


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

    # Пул: ликвидные + растущие за сутки (следим за "муверами")
    by_vol = sorted(tradable, key=lambda s: tickers[s]["quote_volume"], reverse=True)[:40]
    by_chg = sorted([s for s in tradable if 0 < tickers[s]["change_pct"] < 25],
                    key=lambda s: tickers[s]["change_pct"], reverse=True)[:20]
    pool = list(dict.fromkeys(by_vol + by_chg))

    news_items = await fetch_news_cache()
    btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
    btc_ret = _returns([c["close"] for c in btc_candles])

    scored = []
    for sym in pool:
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        if a <= 0 or (a / tickers[sym]["last"]) * 100 < 0.25:
            continue  # слишком низкая волатильность
        score, reasons, keys = score_symbol(candles, tickers[sym], regime)

        # --- Корреляция с BTC/USDT ---
        corr = _corr(_returns([c["close"] for c in candles]), btc_ret)
        if corr > 0.85 and regime == "neutral":
            score -= 1
            reasons.append(f"зеркало BTC (corr {corr:.2f})")
        elif corr < 0.45:
            score += learner.weight("indep")
            reasons.append(f"независима от BTC (corr {corr:.2f})")
            keys.append("indep")

        scored.append({"symbol": sym, "score": score, "reasons": reasons,
                       "reason_keys": keys, "atr": a, "last": tickers[sym]["last"],
                       "liquidity": tickers[sym]["quote_volume"], "corr": round(corr, 2)})

    scored.sort(key=lambda c: c["score"], reverse=True)

    # Диагностика: лучшие сигналы цикла
    thr = threshold(regime)
    SCAN_SUMMARY["text"] = " · ".join(
        f"{c['symbol']} {c['score']:.1f}/{thr}" for c in scored[:3]
    ) or "сигналов нет"
    SCAN_SUMMARY["thr"] = thr
    SCAN_SUMMARY["ts"] = time.time()
    logger.info(f"Scan top: {SCAN_SUMMARY['text']}")

    candidates = []
    for c in scored:
        if c["score"] < thr:
            continue

        # --- НОВОСТНАЯ АНАЛИТИКА (RSS + справка CMC) ---
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
