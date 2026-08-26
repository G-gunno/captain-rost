import time

import httpx
from loguru import logger

from bot.exchange.market_data import market_data
from bot.strategy.indicators import ema, rsi, atr
from bot.strategy.learner import learner
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache, check_sentiment

SCAN_SUMMARY = {"text": "", "thr": 0, "ts": 0}
FILTERED_BY_NEWS = []

SAT_ATR_PCT = 1.2  # порог волатильности: выше — монета идёт в сателлиты

_instruments_cache = {"data": None, "ts": 0}  # кэш инструментов Bybit на 1 час

STABLE_BASES = {"USDC", "USDE", "DAI", "TUSD", "BUSD", "FDUSD", "USDP",
                "USD1", "USDD", "EUR", "EURT", "AEUR", "USDT",
                "RLUSD", "PYUSD", "EURI", "USDS", "USD0", "FRAX",
                "LUSD", "GUSD", "XUSD", "USDX", "CUSD", "SUSD"}

SECTORS = {
    # ===== L1 =====
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "BNB": "L1", "AVAX": "L1",
    "ADA": "L1", "DOT": "L1", "NEAR": "L1", "APT": "L1", "SUI": "L1",
    "SEI": "L1", "TON": "L1", "TRX": "L1", "KAS": "L1", "HBAR": "L1",
    "XLM": "L1", "XRP": "L1", "ALGO": "L1", "ATOM": "L1", "INJ": "L1",
    "TIA": "L1", "ICP": "L1", "FTM": "L1", "S": "L1", "CSPR": "L1",
    "MINA": "L1", "HYPE": "L1", "MOVE": "L1", "MON": "L1", "KAVA": "L1",
    "CELO": "L1", "EGLD": "L1", "VET": "L1", "EOS": "L1", "XTZ": "L1",
    "NEO": "L1", "QTUM": "L1", "WAVES": "L1", "XEM": "L1", "ZEC": "L1",
    "LTC": "L1", "BCH": "L1", "ETC": "L1", "XMR": "L1", "DASH": "L1",
    "ZIL": "L1", "RVN": "L1", "ERG": "L1", "CFX": "L1", "FLR": "L1",
    "KDA": "L1", "ROSE": "L1", "GLMR": "L1", "ASTR": "L1", "METIS": "L1",
    # ===== L2 =====
    "ARB": "L2", "OP": "L2", "STRK": "L2", "ZK": "L2", "MANTA": "L2",
    "SCROLL": "L2", "BLAST": "L2", "POL": "L2", "MATIC": "L2", "ZRO": "L2",
    "MANTLE": "L2", "LINEA": "L2", "IMX": "L2", "LRC": "L2", "STX": "L2",
    # ===== DeFi =====
    "UNI": "DeFi", "AAVE": "DeFi", "LINK": "DeFi", "MKR": "DeFi",
    "SNX": "DeFi", "CRV": "DeFi", "COMP": "DeFi", "LDO": "DeFi",
    "DYDX": "DeFi", "GMX": "DeFi", "JUP": "DeFi", "RAY": "DeFi",
    "PENDLE": "DeFi", "ENA": "DeFi", "ONDO": "DeFi", "PYTH": "DeFi",
    "JTO": "DeFi", "CAKE": "DeFi", "SUSHI": "DeFi", "FLUID": "DeFi",
    "EIGEN": "DeFi", "ETHFI": "DeFi", "RSR": "DeFi", "YFI": "DeFi",
    "BAL": "DeFi", "BNT": "DeFi", "1INCH": "DeFi", "BIFI": "DeFi",
    "KP3R": "DeFi", "RPL": "DeFi", "SSV": "DeFi", "BLUR": "DeFi",
    "MAGIC": "DeFi", "TORN": "DeFi", "AZERO": "DeFi",
    # ===== AI =====
    "FET": "AI", "OCEAN": "AI", "RNDR": "AI", "RENDER": "AI",
    "GRT": "AI", "TAO": "AI", "ARKM": "AI", "WLD": "AI", "VIRTUAL": "AI",
    "FLOCK": "AI", "GRASS": "AI", "SQD": "AI", "AI16Z": "AI",
    "ZEREBRO": "AI", "AGIX": "AI", "AKT": "AI", "NMR": "AI", "PHB": "AI",
    "OLAS": "AI", "PAAL": "AI", "AIOZ": "AI", "CTXC": "AI", "ALCH": "AI",
    "AI": "AI", "COOKIE": "AI",
    # ===== Meme =====
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme",
    "FLOKI": "Meme", "WIF": "Meme", "BRETT": "Meme", "POPCAT": "Meme",
    "MEW": "Meme", "TURBO": "Meme", "PENGU": "Meme", "SPX": "Meme",
    "MOODENG": "Meme", "PUMP": "Meme", "NEIRO": "Meme", "BOME": "Meme",
    "FARTCOIN": "Meme", "PNUT": "Meme", "GOAT": "Meme", "ACT": "Meme",
    "MOTHER": "Meme", "DADDY": "Meme", "GIGA": "Meme",
    "MOG": "Meme", "TOSHI": "Meme", "MYRO": "Meme", "SLERF": "Meme",
    "BODEN": "Meme", "TREMP": "Meme", "HARAMBE": "Meme", "MAGA": "Meme",
    "TRUMP": "Meme", "HAT": "Meme",
    # ===== Gaming =====
    "AXS": "Gaming", "SAND": "Gaming", "MANA": "Gaming", "GALA": "Gaming",
    "RONIN": "Gaming", "PIXEL": "Gaming", "PORTAL": "Gaming",
    "XAI": "Gaming", "NOT": "Gaming", "HMSTR": "Gaming", "CATI": "Gaming",
    "ENJ": "Gaming", "CHZ": "Gaming", "WEMIX": "Gaming", "SUPER": "Gaming",
    "YGG": "Gaming", "BEAM": "Gaming", "GHST": "Gaming",
    "PRIME": "Gaming", "ALT": "Gaming", "ALICE": "Gaming", "BIGTIME": "Gaming",
    # ===== Infra =====
    "FIL": "Infra", "AR": "Infra", "LPT": "Infra", "TWT": "Infra",
    "IOTA": "Infra", "BICO": "Infra", "API3": "Infra", "BAND": "Infra",
    "TRB": "Infra", "HNT": "Infra", "IOTX": "Infra", "XDB": "Infra",
    "WAXP": "Infra", "STORJ": "Infra", "GTC": "Infra", "ANKR": "Infra",
    "HONEY": "Infra", "RAD": "Infra", "MOBILE": "Infra",
    # ===== RWA =====
    "ONDO": "RWA", "PENDLE": "RWA", "ENA": "RWA", "ETHFI": "RWA",
    "MPL": "RWA", "CFG": "RWA", "TOKEN": "RWA", "POLYX": "RWA",
    "CHEX": "RWA", "TRADE": "RWA", "IXT": "RWA", "LPOOL": "RWA",
    # ===== Privacy =====
    "XMR": "Privacy", "ZEC": "Privacy", "DASH": "Privacy", "FIRO": "Privacy",
    "SCRT": "Privacy", "NYM": "Privacy", "TORN": "Privacy", "OASIS": "Privacy",
    # ===== Storage =====
    "FIL": "Storage", "AR": "Storage", "STORJ": "Storage", "SIA": "Storage",
    # ===== DEX =====
    "UNI": "DEX", "CAKE": "DEX", "SUSHI": "DEX", "1INCH": "DEX",
    "DYDX": "DEX", "GMX": "DEX", "JUP": "DEX", "RAY": "DEX",
    "ORCA": "DEX", "OSMO": "DEX",
    # ===== Launchpad =====
    "DAO": "Launchpad", "BOND": "Launchpad",
    # ===== Exchange tokens =====
    "BNB": "Exchange", "KCS": "Exchange", "OKB": "Exchange", "HT": "Exchange",
    "CRO": "Exchange", "GT": "Exchange", "MX": "Exchange", "BGB": "Exchange",
    # ===== Прочее популярное =====
    "BR": "Infra", "XAN": "AI", "OBT": "DeFi", "METAX": "Gaming",
    "ASTER": "L1", "CAP": "DeFi", "TAC": "L1",
    "FF": "Gaming", "BLIFE": "Gaming",
}

SECTOR_LIMITS = {
    "L1": 3, "L2": 3, "DeFi": 3, "AI": 3, "Meme": 3,
    "Gaming": 3, "Infra": 3, "RWA": 2, "Privacy": 2,
    "Storage": 2, "DEX": 3, "Launchpad": 2, "Exchange": 2,
    "Other": 5,
}


def sector_of(base):
    return SECTORS.get(base, "Other")


def sector_limit(sector):
    return SECTOR_LIMITS.get(sector, 3)


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
    return max(4, base + learner.threshold_adj)


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
                for _ in range(5):  # максимум 5 страниц по 1000
                    params = {"category": "spot", "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    r = await c.get("https://api.bybit.com/v5/market/instruments", params=params)
                    result = r.json().get("result", {})
                    raw += result.get("list", [])
                    cursor = result.get("nextPageCursor", "") or ""
                    if not cursor:
                        break
            _instruments_cache["data"], _instruments_cache["ts"] = raw, now
            logger.info(f"Instruments loaded: {len(raw)} spot symbols")
        except Exception as e:
            logger.error(f"Bybit instruments error: {e}")
    now_ms = int(now * 1000)
    out = []
    for ins in raw:
        sym = ins.get("symbol", "")
        if not sym.endswith("USDT") or ins.get("status") != "Trading":
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

    # Базовый пул: ликвидные + растущие за сутки
    by_vol = sorted(tradable, key=lambda s: tickers[s]["quote_volume"], reverse=True)[:40]
    by_chg = sorted([s for s in tradable if 0 < tickers[s]["change_pct"] < 25],
                    key=lambda s: tickers[s]["change_pct"], reverse=True)[:20]

    # Momentum: сильный тренд за неделю
    by_momentum = []
    for sym in tradable[:50]:
        candles = await market_data.get_kline(sym, "60", 168)
        if len(candles) >= 100:
            chg_7d = (candles[-1]["close"] - candles[0]["close"]) / candles[0]["close"] * 100
            if 5 < chg_7d < 50:
                by_momentum.append((sym, chg_7d))
    by_momentum = [s for s, _ in sorted(by_momentum, key=lambda x: x[1], reverse=True)][:15]

    # Volatility: высоковолатильные для сателлитов
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

    # NEW LISTINGS: Bybit API (launchTime) + RSS как дополнение
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
        if sym in seen or not (24 <= age_h <= 336):  # 24ч – 14 дней
            continue
        seen.add(sym)
        if sym in tickers and is_tradable(sym) and tickers[sym]["quote_volume"] >= 500_000:
            by_listings.append((sym, age_h))
            logger.info(f"NEW LISTING: {sym} ({age_h:.1f}h old)")
    by_listings = by_listings[:10]

    # Объединяем пул
    pool = list(dict.fromkeys(by_vol + by_chg + by_momentum + by_volatility +
                              [s for s, _ in by_listings]))

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

        # Корреляция с BTC
        corr = _corr(_returns([c["close"] for c in candles]), btc_ret)
        if corr > 0.85 and regime == "neutral":
            score -= 1
            reasons.append(f"зеркало BTC (corr {corr:.2f})")
        elif corr < 0.45:
            score += learner.weight("indep")
            reasons.append(f"независима от BTC (corr {corr:.2f})")
            keys.append("indep")

        kind = "satellite" if atr_pct >= SAT_ATR_PCT else "core"
        sector = sector_of(sym[:-4])

        scored.append({"symbol": sym, "score": score, "reasons": reasons,
                       "reason_keys": keys, "atr": a, "last": last_price,
                       "liquidity": tickers[sym]["quote_volume"],
                       "corr": round(corr, 2), "atr_pct": round(atr_pct, 2),
                       "kind": kind, "sector": sector})

    scored.sort(key=lambda c: c["score"], reverse=True)

    thr = threshold(regime)
    SCAN_SUMMARY["text"] = " · ".join(
        f"{c['symbol']} {c['score']:.1f}/{thr}" for c in scored[:3]
    ) or "сигналов нет"
    SCAN_SUMMARY["thr"] = thr
    SCAN_SUMMARY["ts"] = time.time()
    logger.info(f"Scan top: {SCAN_SUMMARY['text']}")

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
