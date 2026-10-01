import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.news.rss_news import fetch_news_cache, fetch_listings_cache
from bot.strategy.fundamental import fetch_macro_data

class DataFeederWorker:
    """
    Фоновый воркер-пылесос.
    Каждые 15 минут ходит по внешним API (RSS, DefiLlama, F&G),
    чтобы торговые циклы летали без задержек на I/O.
    """
    def __init__(self, bus: EventBus):
        self.bus = bus

    async def run(self):
        logger.info("📡 Data Feeder Worker запущен: прогрев кэшей API (RSS, Macro)...")
        
        while True:
            try:
                # 1. Принудительно качаем свежие новости (force=True)
                await fetch_news_cache(force=True)
                await fetch_listings_cache(force=True)
                
                # 2. Обновляем макро-метрики (DefiLlama, Fear&Greed)
                await fetch_macro_data()
                
                logger.debug("DataFeeder: Базы новостей и макро-данных успешно обновлены.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DataFeeder error: {e}")
            
            # Спим 15 минут (900 секунд). Сканер будет мгновенно брать данные из кэша.
            await asyncio.sleep(900)
