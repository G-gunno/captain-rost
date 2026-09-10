import asyncio
from loguru import logger
from bot.exchange.market_data import market_data
from bot.core.event_bus import EventBus, Event

class MarketDataWorker:
    def __init__(self, bus: EventBus):
        self.bus = bus

    async def run(self):
        logger.info("📡 Market Data Worker запущен: публикация тикеров (5s)...")
        while True:
            try:
                tickers = await market_data.get_tickers()
                if tickers:
                    # Публикуем событие с актуальными ценами для всех воркеров, кто на это подписан
                    await self.bus.publish(Event(type="PRICE_UPDATED", payload=tickers))
            except Exception as e:
                logger.error(f"MarketDataWorker error: {e}")
            
            # HFT-пульс бота. Каждые 5 секунд шина раздает актуальные цены.
            await asyncio.sleep(5)
