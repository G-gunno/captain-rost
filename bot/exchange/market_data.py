import asyncio
import time
import httpx
from loguru import logger

MAINNET_PUBLIC = "https://api.bybit.com"

_tickers_cache = {}
_tickers_ts = 0


class MarketData:
    """Рыночные данные Bybit (Надежный REST API с кэшем)."""

    def __init__(self, base_url: str = MAINNET_PUBLIC):
        self.base_url = base_url

    async def get_tickers(self) -> dict:
        global _tickers_cache, _tickers_ts
        now = time.time()
        
        # Кэш на 5 секунд: защищает от спама API, если функция вызывается 5 раз за один цикл
        if _tickers_cache and now - _tickers_ts < 5:
            return _tickers_cache
        
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    self.base_url + "/v5/market/tickers", params={"category": "spot"}
                )
            data = resp.json()
            if data.get("retCode") != 0:
                logger.error(f"Tickers error: {data}")
                return _tickers_cache
            
            result = {}
            for t in data.get("result", {}).get("list", []):
                if t["symbol"].endswith("USDT"):
                    result[t["symbol"]] = {
                        "last": float(t["lastPrice"]),
                        "bid1": float(t.get("bid1Price") or t["lastPrice"]),
                        "ask1": float(t.get("ask1Price") or t["lastPrice"]),
                        "quote_volume": float(t.get("turnover24h", 0)),
                        "change_pct": float(t.get("price24hPcnt", 0)) * 100,
                        "high": float(t.get("highPrice24h", 0)),
                        "low": float(t.get("lowPrice24h", 0)),
                    }
            if result:
                _tickers_cache = result
                _tickers_ts = now
            return _tickers_cache
        except Exception as e:
            logger.error(f"REST get_tickers error: {e}")
            return _tickers_cache

    async def get_derivatives_tickers(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    self.base_url + "/v5/market/tickers", params={"category": "linear"}
                )
            data = resp.json()
            if data.get("retCode") != 0:
                return {}
            result = {}
            for t in data.get("result", {}).get("list", []):
                if t["symbol"].endswith("USDT"):
                    result[t["symbol"]] = {
                        "oi": float(t.get("openInterest") or 0),
                        "funding": float(t.get("fundingRate") or 0) * 100,
                    }
            return result
        except Exception:
            return {}

    async def get_kline(self, symbol: str, interval: str = "15", limit: int = 200) -> list:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    self.base_url + "/v5/market/kline",
                    params={"category": "spot", "symbol": symbol, "interval": interval, "limit": limit},
                )
            data = resp.json()
            if data.get("retCode") != 0:
                return []
            candles = []
            for r in data["result"]["list"]:
                candles.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                })
            candles.reverse()
            return candles
        except Exception:
            return []


# Функция-заглушка, чтобы не ломать импорты в main.py
async def start_ws_ticker_stream():
    logger.info("ℹ️ WebSocket стрим отключен. Перешли на пуленепробиваемый REST-кэш (5 сек).")
    while True:
        await asyncio.sleep(3600)


market_data = MarketData()
