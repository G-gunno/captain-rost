from bot.exchange.market_data import market_data
from bot.strategy.indicators import ema, rsi, atr
from bot.news.cmc import get_hype_symbols, get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment

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
    return {"bull": 5, "neutral": 6, "bear": 8}.get(regime, 6)


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

    score, reasons = 0, []
    if last > e50: score += 1; reasons.append("цена выше EMA50")
    if e21 > e50: score += 1; reasons.append("EMA21>EMA50")
    if e12 > e26: score += 1; reasons.append("импульс роста")
    if 40 <= r <= 65: score += 1; reasons.append(f"RSI {r:.0f}")
    if vol_ratio > 1.3: score += 1; reasons.append(f"объём x{vol_ratio:.1f}")
    if 0 < t["change_pct"] < 12: score += 1; reasons.append(f"24ч +{t['change_pct']:.1f}%")
    if regime == "bull": score += 1
    if regime == "bear": score -= 2
    if t["quote_volume"] < 500_000: score -= 1
    return score, reasons


async def scan(regime, tickers, limit=5):
    pre = [s for s, t in tickers.items()
           if is_tradable(s) and t["quote_volume"] >= 200_000 and t["last"] > 0]
    pre.sort(key=lambda s: tickers[s]["quote_volume"], reverse=True)

    hype = await get_hype_symbols()
    news_items = await fetch_news_cache()

    candidates = []
    for sym in pre[:40]:
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        if a <= 0 or (a / tickers[sym]["last"]) * 100 < 0.25:
            continue  # слишком низкая волатильность
        score, reasons = score_symbol(candles, tickers[sym], regime)
        if score < threshold(regime):
            continue

        # --- НОВОСТНАЯ АНАЛИТИКА (CMC + RSS) ---
        base = sym[:-4]
        name = await get_coin_name(base)
        neg, pos, heads = check_sentiment(news_items, [base, name])
        if neg > 0 and neg > pos:
            logger.info(f"{sym}: пропущен из-за негативного новостного фона ({neg})")
            continue
        if pos > neg:
            score += 1
            reasons.append(f"позитивный новостной фон ({pos})")
        if base in hype:
            score += 1
            reasons.append("в трендах CMC")

        candidates.append({
            "symbol": sym, "score": score, "reasons": reasons,
            "atr": a, "last": tickers[sym]["last"],
            "liquidity": tickers[sym]["quote_volume"],
        })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]
