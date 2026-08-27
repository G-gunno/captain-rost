import os
import json
import time
import asyncio
from pathlib import Path

import httpx
from loguru import logger

CMC_BASE = "https://pro-api.coinmarketcap.com"

_cache = {"info": {}}

# ===== БАЗОВЫЙ СЛОВАРЬ (только ~20 топ-монет, которые никогда не поменяются) =====
# Всё остальное выучивается автоматически из CMC тегов и сохраняется в sectors.json
SECTORS = {
    # L1
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "BNB": "L1", "AVAX": "L1",
    "ADA": "L1", "DOT": "L1", "NEAR": "L1", "APT": "L1", "SUI": "L1",
    "XRP": "L1", "LTC": "L1", "BCH": "L1", "ATOM": "L1", "ETC": "L1",
    # L2
    "MATIC": "L2", "POL": "L2", "ARB": "L2", "OP": "L2",
    # DeFi
    "LINK": "DeFi", "UNI": "DeFi", "AAVE": "DeFi", "MKR": "DeFi",
    # Meme
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme",
}

# ===== АВТО-ПЕРЕВОД ТЕГОВ CMC В НАШИ СЕКТОРА =====
SECTOR_TAG_MAP = {
    "layer-1": "L1", "layer-2": "L2", "defi": "DeFi",
    "ai-big-data": "AI", "memes": "Meme", "gaming": "Gaming",
    "metaverse": "Gaming", "nft-collectibles": "Gaming",
    "infrastructure": "Infra", "storage": "Storage", "privacy": "Privacy",
    "real-world-assets": "RWA", "dex": "DEX", "exchange-token": "Exchange",
    "interoperability": "Infra", "oracle": "Infra",
}

SECTOR_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "sectors.json"
_sector_cache = {}


def _load_sectors():
    global _sector_cache
    try:
        if SECTOR_FILE.exists():
            _sector_cache = json.loads(SECTOR_FILE.read_text())
            logger.info(f"sectors: кэш загружен ({len(_sector_cache)} монет)")
    except Exception as e:
        logger.error(f"sectors load error: {e}")


def _save_sectors():
    try:
        SECTOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECTOR_FILE.write_text(json.dumps(_sector_cache, ensure_ascii=False))
    except Exception as e:
        logger.error(f"sectors save error: {e}")


_load_sectors()


def sector_of(base):
    """Синхронная справка сектора: базовый словарь -> выученный кэш -> Other."""
    return SECTORS.get(base) or _sector_cache.get(base) or "Other"


def _tags_to_sector(tags):
    for t in tags or []:
        s = SECTOR_TAG_MAP.get(t)
        if s:
            return s
    return None


async def get_sectors_for_pool(bases):
    """Сектора монет: базовый словарь -> кэш -> теги CMC -> Other.
    Новые монеты выучиваются автоматически и сохраняются в sectors.json."""
    result = {}
    need = []
    for b in bases:
        if b in SECTORS:
            result[b] = SECTORS[b]
        elif b in _sector_cache:
            result[b] = _sector_cache[b]
        else:
            need.append(b)
    if not need:
        return result

    # Защита от rate limit
    await asyncio.sleep(1.0)

    try:
        key = os.getenv("CMC_API_KEY", "").strip()
        headers = {"X-CMC_PRO_API_KEY": key} if key else {}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{CMC_BASE}/v1/cryptocurrency/info",
                            params={"symbol": ",".join(need[:100])},
                            headers=headers)
            data = r.json().get("data", {})
        learned = 0
        for b in need:
            arr = data.get(b)
            sector = "Other"
            if isinstance(arr, list) and arr:
                sector = _tags_to_sector(arr[0].get("tags")) or "Other"
            elif isinstance(arr, dict):
                sector = _tags_to_sector(arr.get("tags")) or "Other"
            result[b] = sector
            # В кэш записываем только реальные секторы, не Other
            # (чтобы при следующем скане попытаться выучить снова)
            if sector != "Other":
                _sector_cache[b] = sector
                learned += 1
        if learned:
            _save_sectors()
            logger.info(f"sectors: авто-выучено {learned} новых монет (кэш: {len(_sector_cache)})")
    except Exception as e:
        logger.error(f"sectors fetch error: {e}")
        for b in need:
            result.setdefault(b, "Other")
    return result


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
        "sectors_learned": len(_sector_cache),
    }
