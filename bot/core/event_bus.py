import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Set
from loguru import logger

@dataclass
class Event:
    type: str
    payload: Any

class EventBus:
    def __init__(self):
        # Храним множество очередей для каждого типа события
        self.subscribers: Dict[str, Set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, event_type: str, maxsize: int = 100) -> asyncio.Queue:
        """
        Регистрация слушателя. maxsize защищает от утечек памяти (OOM) 
        и очередей из устаревших данных (например, потока цен).
        """
        queue = asyncio.Queue(maxsize=maxsize)
        self.subscribers[event_type].add(queue)
        logger.debug(f"EventBus: Создана подписка на событие '{event_type}'")
        return queue

    def publish(self, event_type: str, payload: Any) -> None:
        """
        Синхронный publish (non-blocking). 
        Позволяет публиковать события без await, мгновенно отдавая их в Event Loop.
        """
        if event_type not in self.subscribers:
            return

        event = Event(type=event_type, payload=payload)
        for queue in self.subscribers[event_type]:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Паттерн "Drop Oldest": если очередь полна, выкидываем старое событие,
                # чтобы воркер получил самую актуальную информацию (важно для цен)
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
