import json
import time
from pathlib import Path
from loguru import logger

PAUSED_FILE = Path("storage/paused_orders.json")

class BotState:
    def __init__(self):
        self.trading_enabled = True
        self.paused = False
        self.fomo_cooldowns = {} # Глобальное хранилище штрафов

    def set_cooldown(self, sym: str, seconds: int):
        self.fomo_cooldowns[sym] = int(time.time()) + seconds

    def is_on_cooldown(self, sym: str) -> bool:
        if sym not in self.fomo_cooldowns:
            return False
        if time.time() > self.fomo_cooldowns[sym]:
            del self.fomo_cooldowns[sym]
            return False
        return True

    def pause(self, orders):
        self.paused = True
        try:
            PAUSED_FILE.parent.mkdir(parents=True, exist_ok=True)
            PAUSED_FILE.write_text(json.dumps(orders, ensure_ascii=False))
        except Exception as e:
            logger.error(f"pause save error: {e}")

    def resume(self):
        self.paused = False
        orders = []
        try:
            if PAUSED_FILE.exists():
                orders = json.loads(PAUSED_FILE.read_text())
                PAUSED_FILE.unlink()
        except Exception as e:
            logger.error(f"resume load error: {e}")
        return orders

    def fresh_start(self):
        self.paused = False
        self.trading_enabled = True
        self.fomo_cooldowns.clear()
        try:
            if PAUSED_FILE.exists():
                PAUSED_FILE.unlink()
        except Exception:
            pass

bot_state = BotState()
