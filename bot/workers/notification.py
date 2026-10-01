import asyncio
from loguru import logger
from bot.core.event_bus import EventBus

class NotificationWorker:
    """Асинхронная очередь уведомлений Telegram с защитой от превышения лимитов."""
    
    def __init__(self, bus: EventBus, send_chat_func):
        self.bus = bus
        self.send_chat = send_chat_func
        self.queue = self.bus.subscribe("NOTIFY", maxsize=200)
        
        # Лимит одного сообщения в Telegram - 4096 символов. 
        # Берем 3800 с запасом на HTML-теги и форматирование.
        self.MAX_MSG_LENGTH = 3800

    async def run(self):
        logger.info("📬 Notification Worker запущен: умный батчинг сообщений")
        buffer = []
        current_length = 0
        
        while True:
            try:
                # Ждем сообщение с таймаутом (2 сек), чтобы периодически сбрасывать буфер
                try:
                    event = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                    payload = event.payload
                    
                    text = payload.get("text", "")
                    urgent = payload.get("urgent", False)
                    
                    if urgent:
                        # Экстренные сообщения (паника/дамп) отправляем немедленно
                        await self._send_chunks(text)
                    else:
                        text_len = len(text) + 2  # +2 для разделителя \n\n
                        
                        # Если буфер переполняется - отправляем то, что накопили
                        if current_length + text_len > self.MAX_MSG_LENGTH:
                            await self._flush(buffer)
                            buffer = []
                            current_length = 0
                            
                        buffer.append(text)
                        current_length += text_len
                        
                        # Батчинг по 5 сообщений для снижения количества запросов к API
                        if len(buffer) >= 5:
                            await self._flush(buffer)
                            buffer = []
                            current_length = 0
                            
                    self.queue.task_done()
                except asyncio.TimeoutError:
                    # Раз в 2 секунды сбрасываем всё, что зависло в буфере
                    if buffer:
                        await self._flush(buffer)
                        buffer = []
                        current_length = 0
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"NotificationWorker error: {e}")

    async def _flush(self, buffer: list):
        """Склеивает и отправляет накопившийся буфер."""
        if not buffer:
            return
        msg = "\n\n".join(buffer)
        await self._send_chunks(msg)

    async def _send_chunks(self, text: str):
        """Безопасная отправка длинных сообщений с разбивкой."""
        while len(text) > 0:
            chunk = text[:self.MAX_MSG_LENGTH]
            
            if len(text) > self.MAX_MSG_LENGTH:
                # Ищем последний перенос строки, чтобы не рвать слова и HTML-теги пополам
                last_newline = chunk.rfind('\n')
                if last_newline > 0:
                    chunk = chunk[:last_newline]
            
            try:
                await self.send_chat(chunk)
            except Exception as e:
                logger.error(f"Telegram send chunk error: {e}")
                
            text = text[len(chunk):].lstrip()
            # Защита от Flood Limit'ов Telegram API (не более 20 сообщений в минуту в 1 чат)
            await asyncio.sleep(0.2)
