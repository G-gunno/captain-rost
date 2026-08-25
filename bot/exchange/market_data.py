import httpx
from loguru import logger

MAINNET_PUBLIC = "https://api.bybit.com"


class MarketData:
    """Публичные рыночные данные Bybit (без API-ключа)."""

    def __init__(self, base_url: str = MAINNET_PUBLIC):
        self.base_url = base_url

    async def get_tickers(self) -> dict:
        """{symbol: {last, quote_volume, change_pct, high, low}} по всем парам USDT."""
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
                    "quote_volume": float(t.get("turnover24h", 0)),
                    "change_pct": float(t.get("price24hPcnt", 0)) * 100,
                    "high": float(t.get("highPrice24h", 0)),
                    "low": float(t.get("lowPrice24h", 0)),
                }
        return result

    async def get_kline(self, symbol: str, interval: str = "15", limit: int = 200) -> list:
        """Свечи по возрастанию времени: [{ts, open, high, low, close, volume}, ...]"""
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                self.base_url + "/v5/market/kline",
                params={"category": "spot", "symbol": symbol, "interval": interval, "limit": limit},
            )
        data = resp.json()
        if data.get("retCode") != 0:
            logger.error(f"Kline error {symbol}: {data}")
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


market_data = MarketData()
