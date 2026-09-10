import asyncio
import time
from loguru import logger
from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper

class ExecutionRiskWorker:
    """Критический воркер. Отвечает только за сдвиг стопов и экстренный выход. Нулевая задержка."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED")
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP")

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: ожидание тикеров для контроля рисков...")
        
        while True:
            price_task = asyncio.create_task(self.price_queue.get())
            emerg_task = asyncio.create_task(self.emergency_queue.get())
            
            done, pending = await asyncio.wait(
                [price_task, emerg_task], 
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()

            for task in done:
                event = task.result()
                if event.type == "PRICE_UPDATED":
                    await self._process_tick(event.payload)
                    self.price_queue.task_done()
                elif event.type == "EMERGENCY_DUMP":
                    await self._panic_sell(event.payload)
                    self.emergency_queue.task_done()

    async def _process_tick(self, tickers: dict) -> None:
        """
        Мгновенная проверка позиций на тейк-профит или сдвиг стоп-лосса.
        Пока работает в холостом режиме (ничего не двигает).
        """
        # Убедимся, что цены доходят моментально
        btc_price = tickers.get("BTCUSDT", {}).get("last", 0)
        # Раскомментируй строку ниже, чтобы увидеть поток цен в консоли:
        # logger.debug(f"Execution: Получен тик цен. BTC = {btc_price}")
        pass

    async def _panic_sell(self, tickers: dict) -> None:
        logger.critical("🚨 Execution Worker: АКТИВИРОВАН ЭКСТРЕННЫЙ ВЫХОД! Продаю всё!")
        results = paper.sell_all(tickers)
        for ex in results:
            logger.info(f"Panic Sell: {ex['symbol']} PnL: {ex['pnl_pct']:.2f}%")
