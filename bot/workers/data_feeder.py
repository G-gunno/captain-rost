import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache
from bot.strategy.fundamental import _fetch_stablecoin_flows, _fetch_defillama_sectors, _fetch_fear_and_greed

class DataFeederWorker:
    """
    Изолированный фоновый процесс.
    Крутится сам по себе раз в 5 минут и 'прогревает' все тяжелые кэши (RSS, Макро).
    Благодаря этому, Сканер получает эти данные из памяти (RAM) с нулевой задержкой!
    """
    def __init__(self, bus: EventBus):
        self.bus = bus

    async def run(self):
        logger.info("📡 Data Feeder Worker запущен: фоновый прогрев кэшей (RSS, Макро)")
        await asyncio.sleep(5) # Даем боту плавно запуститься
        
        while True:
            try:
                # Запускаем все парсеры параллельно в фоне. 
                # Они сами обновят свои внутренние словари _cache.
                await asyncio.gather(
                    fetch_news_cache(),
                    fetch_listings_cache(),
                    _fetch_stablecoin_flows(),
                    _fetch_defillama_sectors(),
                    _fetch_fear_and_greed(),
                    return_exceptions=True # Чтобы падение одного сайта не убило остальные
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DataFeederWorker error: {e}")
            
            # Спим 5 минут. Кэш живет 15 минут, поэтому он никогда не успеет протухнуть.
            await asyncio.sleep(300)
