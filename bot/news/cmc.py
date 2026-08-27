import os
import json
import time
from pathlib import Path

import httpx
from loguru import logger

CMC_BASE = "https://pro-api.coinmarketcap.com"

_cache = {"info": {}}

# ===== РУЧНОЙ СЛОВАРЬ (приоритетный оверрид) =====
SECTORS = {
    "BTC": "L1", "ETH": "L1", "SOL": "L1", "BNB": "L1", "AVAX": "L1",
    "ADA": "L1", "DOT": "L1", "NEAR": "L1", "APT": "L1", "SUI": "L1",
    "SEI": "L1", "TON": "L1", "TRX": "L1", "KAS": "L1", "HBAR": "L1",
    "XLM": "L1", "XRP": "L1", "ALGO": "L1", "ATOM": "L1", "INJ": "L1",
    "TIA": "L1", "ICP": "L1", "FTM": "L1", "S": "L1", "CSPR": "L1",
    "MINA": "L1", "HYPE": "L1", "MOVE": "L1", "MON": "L1", "KAVA": "L1",
    "CELO": "L1", "EGLD": "L1", "VET": "L1", "EOS": "L1", "XTZ": "L1",
    "LTC": "L1", "BCH": "L1", "ETC": "L1", "ZIL": "L1", "RVN": "L1",
    "GLMR": "L1", "ASTR": "L1", "MERL": "L1", "OBT": "L1", "BR": "L1",
    "XAN": "L1", "CAP": "L1", "TAC": "L1", "FF": "L1", "ASTER": "L1",
    "ARB": "L2", "OP": "L2", "STRK": "L2", "ZK": "L2", "MANTA": "L2",
    "SCROLL": "L2", "BLAST": "L2", "POL": "L2", "MATIC": "L2", "ZRO": "L2",
    "MANTLE": "L2", "LINEA": "L2", "IMX": "L2", "STX": "L2",
    "UNI": "DeFi", "AAVE": "DeFi", "LINK": "DeFi", "MKR": "DeFi",
    "SNX": "DeFi", "CRV": "DeFi", "COMP": "DeFi", "LDO": "DeFi",
    "DYDX": "DeFi", "GMX": "DeFi", "JUP": "DeFi", "RAY": "DeFi",
    "PENDLE": "DeFi", "ENA": "DeFi", "ONDO": "DeFi", "PYTH": "DeFi",
    "JTO": "DeFi", "CAKE": "DeFi", "SUSHI": "DeFi", "FLUID": "DeFi",
    "EIGEN": "DeFi", "ETHFI": "DeFi", "RSR": "DeFi", "BICO": "DeFi",
    "TWT": "Infra", "FIL": "Infra", "AR": "Infra", "LPT": "Infra",
    "IOTA": "Infra", "API3": "Infra", "BAND": "Infra", "TRB": "Infra",
    "HNT": "Infra", "IOTX": "Infra", "WAXP": "Infra", "STORJ": "Infra",
    "ANKR": "Infra", "RAD": "Infra", "MOBILE": "Infra",
    "FET": "AI", "OCEAN": "AI", "RNDR": "AI", "GRT": "AI", "TAO": "AI",
    "ARKM": "AI", "WLD": "AI", "VIRTUAL": "AI", "FLOCK": "AI",
    "GRASS": "AI", "SQD": "AI", "ZEREBRO": "AI", "COOKIE": "AI",
    "METAX": "AI", "CHIP": "AI",
    "DOGE": "Meme", "SHIB": "Meme", "PEPE": "Meme", "BONK": "Meme",
    "FLOKI": "Meme", "WIF": "Meme", "BRETT": "Meme", "POPCAT": "Meme",
    "MEW": "Meme", "TURBO": "Meme", "PENGU": "Meme", "SPX": "Meme",
    "MOODENG": "Meme", "PUMP": "Meme", "NEIRO": "Meme", "BOME": "Meme",
    "FARTCOIN": "Meme", "PNUT": "Meme", "GOAT": "Meme", "ACT": "Meme",
    "TRUMP": "Meme", "HAT": "Meme",
    "AXS": "Gaming", "SAND": "Gaming", "MANA": "Gaming", "GALA": "Gaming",
    "RONIN": "Gaming", "PIXEL": "Gaming", "PORTAL": "Gaming",
    "XAI": "Gaming", "NOT": "Gaming", "HMSTR": "Gaming", "CATI": "Gaming",
    "ENJ": "Gaming", "CHZ": "Gaming", "SUPER": "Gaming", "YGG": "Gaming",
    "BEAM": "Gaming", "GHST": "Gaming", "PRIME": "Gaming", "ALICE": "Gaming",
    "BIGTIME": "Gaming", "BSB": "Gaming",
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


def _tags_to_sector(tags):
    for t in tags or []:
        s = SECTOR_TAG_MAP.get(t)
        if s:
            return s
    return None


async def get_sectors_for_pool(bases):
    """Сектора монет: ручной словарь -> кэш -> теги CMC -> Other. Авто-обучение."""
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
            _sector_cache[b] = sector
            learned += 1
        _save_sectors()
        logger.info(f"sectors: авто-выучено {learned} монет")
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
