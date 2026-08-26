import os
import time

import httpx
from loguru import logger

CMC_BASE = "https://pro-api.coinmarketcap.com"

_cache = {"info": {}}


async def _get(path, params):
    key = os.getenv("CMC_API_KEY", "").strip()
    if not key:
        return None
    headers = {"X-CMC_PRO_API_KEY": key}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(CMC_BASE + path, params=params, headers=headers)
        data = r.json()
        if data.get("status", {}).get("error_code"):
            logger.error(f"CMC error: {data['status']}")
            return None
        return data
    except Exception as e:
        logger.error(f"CMC request error: {e}")
        return None


async def get_coin_name(symbol: str) -> str:
    """Название монеты по тику (кэш 24 часа)."""
    info = _cache["info"].get(symbol)
    if info and time.time() - info["ts"] < 86400:
        return info["name"]
    data = await _get("/v1/cryptocurrency/info", {"symbol": symbol})
    name = ""
    if data:
        arr = data.get("data", {}).get(symbol) or data.get("data", {}).get(symbol.upper())
        if isinstance(arr, list) and arr:
            name = arr[0].get("name", "")
        elif isinstance(arr, dict):
            name = arr.get("name", "")
    _cache["info"][symbol] = {"name": name, "ts": time.time()}
    return name


def get_stats():
    """Статистика CMC API для мониторинга."""
    key = os.getenv("CMC_API_KEY", "").strip()
    return {
        "api_key_set": bool(key),
        "cache_count": len(_cache["info"]),
    }
