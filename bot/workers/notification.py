import asyncio
from loguru import logger
from bot.core.event_bus import EventBus

class NotificationWorker:
    """Асинхронная очередь уведомлений Telegram (защита от сетевых задержек)."""
    
    def __init__(self, bus: EventBus, send_chat_func):
        self.bus = bus
        self.send_chat = send_chat_func
        self.queue = self.bus.subscribe("NOTIFY", maxsize=100)

    async def run(self):
        logger.info("📬 Notification Worker запущен: асинхронная очередь сообщений")
        buffer = []
        
        while True:
            try:
                # Ждем сообщение с таймаутом (2 сек), чтобы периодически сбрасывать буфер
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                    payload = event.payload
                    
                    text = payload.get("text", "")
                    urgent = payload.get("urgent", False)
                    
                    if urgent:
                        await self.send_chat(text)
                    else:
                        buffer.append(text)
                        
                    self.queue.task_done()
                except asyncio.TimeoutError:
                    pass # Сработал таймаут, идем проверять буфер
                
                # Отправляем склеенный буфер (до 5 сообщений в одном)
                if buffer and (len(buffer) >= 5 or self.queue.empty()):
                    msg = "\n\n".join(buffer)
                    await self.send_chat(msg)
                    buffer.clear()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"NotificationWorker error: {e}")
