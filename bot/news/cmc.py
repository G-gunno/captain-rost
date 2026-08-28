import os
import json
import time
import asyncio
from pathlib import Path

import httpx
from loguru import logger

from bot.core.remote_state import download_state, upload_state

CMC_BASE = "https://pro-api.coinmarketcap.com"

_cache = {"info": {}}

# ===== БАЗОВЫЙ СЛОВАРЬ СЕКТОРОВ (только оверрид; остальное учится само) =====
SECTORS = {
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "BNB": "L1", "AVAX": "L1",
    "ADA": "L1", "DOT": "L1", "NEAR": "L1", "APT": "L1", "SUI": "L1",
    "XRP": "L1", "LTC": "L1", "BCH": "L1", "ATOM": "L1", "TON": "L1",
    "ARB": "L2", "OP": "L2", "STRK": "L2", "POL": "L2", "ZRO": "L2",
    "UNI": "DeFi", "AAVE": "DeFi", "LINK": "DeFi", "MKR": "DeFi",
    "CRV": "DeFi", "LDO": "DeFi", "DYDX": "DeFi", "JUP": "DeFi",
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme",
    "WIF": "Meme", "FLOKI": "Meme", "PENGU": "Meme", "TRUMP": "Meme",
    "AXS": "Gaming", "SAND": "Gaming", "GALA": "Gaming", "NOT": "Gaming",
    "FET": "AI", "RNDR": "AI", "GRT": "AI", "WLD": "AI", "GRASS": "AI",
}

SECTOR_TAG_MAP = {
    "layer-1": "L1", "layer-2": "L2", "defi": "DeFi",
    "ai-big-data": "AI", "memes": "Meme", "gaming": "Gaming",
    "metaverse": "Gaming", "nft-collectibles": "Gaming",
    "infrastructure": "Infra", "storage": "Storage", "privacy": "Privacy",
    "real-world-assets": "RWA", "dex": "DEX", "exchange-token": "Exchange",
    "interoperability": "Infra", "oracle": "Infra",
}

# ===== КАП-ТИРЫ CMC =====
TIER_EMOJI = {"TOP20": "🐋", "MID": "🐘", "SMALL": "🐅", "MICRO": "🐭"}
TIER_NAMES = {
    "TOP20": "Топ-20 · киты",
    "MID": "21–100 · слоны",
    "SMALL": "101–500 · тигры",
    "MICRO": "500+ · мыши",
}


def tier_of(rank):
    if rank is None:
        return "MICRO"
    if rank <= 20:
        return "TOP20"
    if rank <= 100:
        return "MID"
    if rank <= 500:
        return "SMALL"
    return "MICRO"


# ===== КЭШ СЕКТОРОВ (локально + бэкап в GitHub) =====
SECTOR_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "sectors.json"
SECTOR_REMOTE = "sectors.json"
_sector_cache = {}
_last_upload = 0.0


def _load_sectors():
    global _sector_cache
    try:
        if SECTOR_FILE.exists():
            _sector_cache = json.loads(SECTOR_FILE.read_text())
            logger.info(f"sectors: кэш загружен ({len(_sector_cache)} монет)")
    except Exception as e:
        logger.error(f"sectors load error: {e}")
    # Если локального файла нет (свежий деплой) — восстанавливаем из GitHub
    if not _sector_cache:
        data = download_state(SECTOR_REMOTE)
        if isinstance(data, dict) and data:
            _sector_cache = data
            logger.info(f"sectors: кэш восстановлен из GitHub ({len(_sector_cache)} монет)")


def _save_sectors():
    global _last_upload
    try:
        SECTOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECTOR_FILE.write_text(json.dumps(_sector_cache, ensure_ascii=False))
    except Exception as e:
        logger.error(f"sectors save error: {e}")
    # Бэкап в GitHub не чаще раза в 60 секунд
    if time.time() - _last_upload > 60:
        _last_upload = time.time()
        upload_state(SECTOR_REMOTE, _sector_cache)


_load_sectors()


def sector_of(base):
    return SECTORS.get(base) or _sector_cache.get(base) or "Other"


def _tags_to_sector(tags):
    for t in tags or []:
        s = SECTOR_TAG_MAP.get(t)
        if s:
            return s
    return None


async def get_sectors_for_pool(bases):
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


# ===== РАНГИ CMC (кэш 6 часов) =====
RANKS_FILE = Path(os.getenv("STORAGE_DIR", "storage")) / "ranks.json"
_ranks = {"data": {}, "ts": 0.0}


def _load_ranks():
    try:
        if RANKS_FILE.exists():
            d = json.loads(RANKS_FILE.read_text())
            _ranks["data"] = d.get("data", {})
            _ranks["ts"] = float(d.get("ts", 0.0))
            logger.info(f"ranks: кэш загружен ({len(_ranks['data'])} рангов)")
    except Exception as e:
        logger.error(f"ranks load error: {e}")


_load_ranks()


async def get_ranks_for_pool(bases):
    """Ранги CMC для пула; обновление раз в 6 часов (1 пакетный запрос)."""
    now = time.time()
    if _ranks["data"] and now - _ranks["ts"] < 21600:
        return {b: _ranks["data"].get(b) for b in bases}
    fetched = {}
    try:
        key = os.getenv("CMC_API_KEY", "").strip()
        headers = {"X-CMC_PRO_API_KEY": key} if key else {}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{CMC_BASE}/v1/cryptocurrency/listings/latest",
                params={"symbol": ",".join(bases[:100]), "limit": 200},
                headers=headers,
            )
            data = r.json().get("data", [])
        for item in data:
            fetched[item.get("symbol")] = item.get("cmc_rank")
        _ranks["data"].update(fetched)
        _ranks["ts"] = now
        try:
            RANKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            RANKS_FILE.write_text(json.dumps({"data": _ranks["data"], "ts": _ranks["ts"]}))
        except Exception as e:
            logger.error(f"ranks save error: {e}")
        logger.info(f"ranks: обновлено {len(fetched)} рангов CMC")
    except Exception as e:
        logger.error(f"ranks fetch error: {e}")
    return {b: fetched.get(b, _ranks["data"].get(b)) for b in bases}


# ===== ПАМЯТЬ ПО МОНЕТАМ (для /learn) =====
def memory_stats():
    """Сводка памяти: база + выученные монеты, раскладка по секторам и тирам."""
    sector_counts = {}
    for sec in SECTORS.values():
        sector_counts[sec] = sector_counts.get(sec, 0) + 1
    learned = 0
    for base, sec in _sector_cache.items():
        if base not in SECTORS:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
            learned += 1
    all_bases = set(SECTORS.keys()) | set(_sector_cache.keys())
    tier_counts = {}
    for base in all_bases:
        t = tier_of(_ranks["data"].get(base))
        tier_counts[t] = tier_counts.get(t, 0) + 1
    return {
        "base": len(SECTORS),
        "learned": learned,
        "total": len(all_bases),
        "sectors": sector_counts,
        "tiers": tier_counts,
    }


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
    key = os.getenv("CMC_API_KEY", "").strip()
    return {
        "api_key_set": bool(key),
        "cache_count": len(_cache["info"]),
        "sectors_learned": len(_sector_cache),
        "ranks_cached": len(_ranks["data"]),
    }
