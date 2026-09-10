import asyncio
from loguru import logger
from bot.exchange.market_data import market_data
from bot.core.event_bus import EventBus, Event

class MarketDataWorker:
    """Фоновый воркер. Забирает цены с биржи и кидает в шину событий."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus

    async def run(self):
        logger.info("📡 Market Data Worker запущен: публикация тикеров (каждые 5s)...")
        while True:
            try:
                tickers = await market_data.get_tickers()
                if tickers:
                    # Рассылаем тикеры всем подписанным воркерам (Execution, Scanner и т.д.)
                    await self.bus.publish(Event(type="PRICE_UPDATED", payload=tickers))
            except Exception as e:
                logger.error(f"MarketDataWorker error: {e}")
            
            await asyncio.sleep(5)
