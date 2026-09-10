import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List

@dataclass
class Event:
    type: str
    payload: Any

class EventBus:
    def __init__(self) -> None:
        self.subscribers: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, event_type: str) -> asyncio.Queue:
        """Регистрация очереди для конкретного типа события."""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        queue = asyncio.Queue()
        self.subscribers[event_type].append(queue)
        return queue

    async def publish(self, event: Event) -> None:
        """Мгновенная рассылка события всем подписчикам."""
        if event.type in self.subscribers:
            for queue in self.subscribers[event.type]:
                await queue.put(event)
