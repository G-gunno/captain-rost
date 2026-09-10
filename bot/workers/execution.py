import asyncio
from loguru import logger
from bot.core.event_bus import EventBus

class ExecutionRiskWorker:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED")
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP")

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: ожидание цен...")
        
        while True:
            # Используем asyncio.wait, чтобы слушать обе очереди одновременно
            price_task = asyncio.create_task(self.price_queue.get())
            emerg_task = asyncio.create_task(self.emergency_queue.get())
            
            done, pending = await asyncio.wait(
                [price_task, emerg_task], 
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()  # Отменяем то, что не сработало

            for task in done:
                event = task.result()
                if event.type == "PRICE_UPDATED":
                    await self._process_tick(event.payload)
                    self.price_queue.task_done()
                elif event.type == "EMERGENCY_DUMP":
                    await self._panic_sell()
                    self.emergency_queue.task_done()

    async def _process_tick(self, data: dict) -> None:
        """Здесь живет миллисекундная логика трейлинг-стопов и фиксации TP."""
        # sym = data['symbol']
        # price = data['last']
        pass

    async def _panic_sell(self) -> None:
        """Сброс всех позиций по рынку."""
        logger.critical("🚨 АКТИВИРОВАН ЭКСТРЕННЫЙ ВЫХОД!")
