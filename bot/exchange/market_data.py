import asyncio
import json
import httpx
from loguru import logger
import websockets

MAINNET_PUBLIC = "https://api.bybit.com"
WS_PUBLIC_SPOT = "wss://stream.bybit.com/v5/public/spot"

_ws_tickers = {}
_ws_running = False


class MarketData:
    """Рыночные данные Bybit (REST + Real-time WebSockets)."""

    def __init__(self, base_url: str = MAINNET_PUBLIC):
        self.base_url = base_url

    async def get_tickers(self) -> dict:
        """Возвращает актуальные тикеры. Сначала из WS-кэша, если пусто — fallback на REST."""
        if _ws_tickers:
            return _ws_tickers
        
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                self.base_url + "/v5/market/tickers", params={"category": "spot"}
            )
        data = resp.json()
        if data.get("retCode") != 0:
            logger.error(f"Tickers error: {data}")
            return {}
        result = {}
        for t in data["result"]["list"]:
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
        return result

    async def get_derivatives_tickers(self) -> dict:
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

    async def get_kline(self, symbol: str, interval: str = "15", limit: int = 200) -> list:
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


async def start_ws_ticker_stream():
    """Фоновый поток для приема тикеров по WebSocket в реальном времени."""
    global _ws_tickers, _ws_running
    if _ws_running:
        return
    _ws_running = True

    # Сначала подгружаем базовый кэш через REST, чтобы не ждать старта WS
    try:
        initial = await market_data.get_tickers()
        if initial:
            _ws_tickers.update(initial)
    except Exception:
        pass

    while True:
        try:
            async with websockets.connect(WS_PUBLIC_SPOT) as ws:
                # Подписываемся на поток all tickers
                sub_msg = {"op": "subscribe", "args": ["tickers.all"]}
                await ws.send(json.dumps(sub_msg))
                logger.info("✅ WebSocket подключен к потоку спот-тикеров Bybit")

                async for message in ws:  # <--- ИСПРАВЛЕНА ОШИБКА СИНТАКСИСА ЗДЕСЬ
                    data = json.loads(message)
                    if "topic" in data and data["topic"].startswith("tickers."):
                        d = data.get("data", {})
                        sym = d.get("symbol")
                        if sym and sym.endswith("USDT"):
                            # Обновляем точечно конкретный тикер
                            current = _ws_tickers.get(sym, {})
                            _ws_tickers[sym] = {
                                "last": float(d.get("lastPrice") or current.get("last", 0)),
                                "bid1": float(d.get("bid1Price") or current.get("bid1", 0)),
                                "ask1": float(d.get("ask1Price") or current.get("ask1", 0)),
                                "quote_volume": float(d.get("turnover24h") or current.get("quote_volume", 0)),
                                "change_pct": float(d.get("price24hPcnt") or 0) * 100,
                                "high": float(d.get("highPrice24h") or current.get("high", 0)),
                                "low": float(d.get("lowPrice24h") or current.get("low", 0)),
                            }
        except Exception as e:
            logger.warning(f"WebSocket обрыв связи: {e}. Переподключение через 5 сек...")
            await asyncio.sleep(5)


market_data = MarketData()
