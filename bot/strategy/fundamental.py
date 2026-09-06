import os
import time
import asyncio
import httpx
from loguru import logger

# Глобальный кэш фундаментальных данных
_cache = {
    "stablecoins": {"trend": "neutral", "ts": 0},
    "hot_sectors": {"data": [], "ts": 0},
    "fear_and_greed": {"value": 50, "ts": 0}  # <-- НОВОЕ
}

CACHE_TTL = 3600  # Обновлять раз в 1 час


async def _fetch_stablecoin_flows():
    """Анализирует приток/отток стейблкоинов (DefiLlama - Бесплатно)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://stablecoins.llama.fi/stablecoincharts/all")
        data = r.json()
        if len(data) >= 8:
            today = data[-1]["totalCirculatingUSD"]["peggedUSD"]
            week_ago = data[-8]["totalCirculatingUSD"]["peggedUSD"]
            change_pct = (today - week_ago) / week_ago * 100
            
            if change_pct > 0.5:
                _cache["stablecoins"]["trend"] = "bull"
            elif change_pct < -0.5:
                _cache["stablecoins"]["trend"] = "bear"
            else:
                _cache["stablecoins"]["trend"] = "neutral"
            
            logger.info(f"DefiLlama | Stablecoin Flow: {change_pct:+.2f}% -> {_cache['stablecoins']['trend']}")
    except Exception as e:
        logger.debug(f"DefiLlama API error: {e}")


async def _fetch_defillama_sectors():
    """Смотрит, куда перетекает TVL по чейнам/секторам (DefiLlama - Бесплатно)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.llama.fi/v2/chains")
        data = r.json()
        hot = []
        for chain in data:
            if chain.get("tvl", 0) > 100_000_000:
                chg = chain.get("tvlChange_7d", 0) or 0
                if chg > 10.0:  
                    name = chain["name"].upper()
                    if name in ("ETHEREUM", "SOLANA", "AVALANCHE", "SUI", "APTOS", "NEAR"):
                        hot.append("L1")
                    elif name in ("ARBITRUM", "OPTIMISM", "BASE", "POLYGON", "STARKNET"):
                        hot.append("L2")
        
        _cache["hot_sectors"]["data"] = list(set(hot))
        if hot:
            logger.info(f"DefiLlama | Hot Sectors (TVL Inflow): {', '.join(set(hot))}")
    except Exception as e:
        logger.debug(f"DefiLlama Chains API error: {e}")


async def _fetch_fear_and_greed():
    """Запрашивает индекс Страха и Жадности (Alternative.me - Бесплатно, без ключа)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.alternative.me/fng/?limit=1")
        data = r.json()
        val = int(data["data"][0]["value"])
        _cache["fear_and_greed"]["value"] = val
        logger.info(f"Alternative.me | Fear & Greed Index: {val}")
    except Exception as e:
        logger.debug(f"Fear & Greed API error: {e}")


# ================= ПУБЛИЧНЫЕ МЕТОДЫ =================

async def update_fundamental_data():
    """Фоновый воркер (вызывается 1 раз в час из main.py)."""
    while True:
        logger.info("📡 Сбор фундаментальных макро-данных (DefiLlama, F&G)...")
        await asyncio.gather(
            _fetch_stablecoin_flows(),
            _fetch_defillama_sectors(),
            _fetch_fear_and_greed()
        )
        await asyncio.sleep(CACHE_TTL)

def get_macro_trend():
    return _cache["stablecoins"]["trend"]

def is_sector_hot(sector):
    return sector in _cache["hot_sectors"]["data"]

def get_fear_and_greed():
    return _cache["fear_and_greed"]["value"]


# ================= ON-DEMAND ПРОВЕРКИ =================

async def check_coinglass_liquidation_threat(symbol):
    """Проверяет риск сквиза по Coinglass (Требуется COINGLASS_API_KEY)."""
    api_key = os.getenv("COINGLASS_API_KEY")
    if not api_key:
        return False
        
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    try:
        headers = {"coinglassSecret": api_key}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://open-api.coinglass.com/public/v2/indicator/long_short_ratio", 
                            params={"symbol": base, "time_type": "h1"}, headers=headers)
        data = r.json().get("data", [])
        # Если лонгистов подавляющее большинство (кэф > 3.0), маркетмейкер побреет их вниз.
        if data and data[-1].get("longShortRatio", 1.0) > 3.0:
            return True 
    except Exception as e:
        logger.debug(f"Coinglass API error: {e}")
    return False
