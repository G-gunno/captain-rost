import asyncio
import time
import httpx
from loguru import logger

MAINNET_PUBLIC = "https://api.bybit.com"

_tickers_cache = {}
_tickers_ts = 0
_kline_cache = {} # <-- ДОБАВИЛИ КЭШ ДЛЯ СВЕЧЕЙ

class MarketData:
    """Рыночные данные Bybit (Надежный REST API с пулом соединений)."""

    def __init__(self, base_url: str = MAINNET_PUBLIC):
        self.base_url = base_url
        self._client = None

    @property
    def client(self):
        # Ленивая инициализация клиента для удержания единого TCP/TLS туннеля (Keep-Alive)
        if self._client is None:
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=20,
                limits=limits
            )
        return self._client

    async def get_tickers(self) -> dict:
        global _tickers_cache, _tickers_ts
        now = time.time()
        
        if _tickers_cache and now - _tickers_ts < 5:
            return _tickers_cache
        
        try:
            resp = await self.client.get("/v5/market/tickers", params={"category": "spot"})
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
            resp = await self.client.get("/v5/market/tickers", params={"category": "linear"})
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
        global _kline_cache
        cache_key = f"{symbol}_{interval}_{limit}"
        now = time.time()
        
        # Часовики ("60") кэшируем на 15 минут (900 сек). 15-минутки не кэшируем.
        ttl = 900 if interval == "60" else 0
        
        if ttl > 0 and cache_key in _kline_cache and now - _kline_cache[cache_key]['ts'] < ttl:
            return _kline_cache[cache_key]['data']

        try:
            resp = await self.client.get(
                "/v5/market/kline",
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
            
            # Сохраняем в кэш, если это часовик
            if ttl > 0:
                _kline_cache[cache_key] = {'data': candles, 'ts': now}
                
            return candles
        except Exception:
            return []

async def start_ws_ticker_stream():
    logger.info("ℹ️ WebSocket стрим отключен. Перешли на пуленепробиваемый REST-кэш (5 сек).")
    while True:
        await asyncio.sleep(3600)

market_data = MarketData()
