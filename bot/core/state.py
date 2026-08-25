import json
from pathlib import Path

from loguru import logger

PAUSED_FILE = Path("storage/paused_orders.json")


class BotState:
    def __init__(self):
        self.trading_enabled = True
        self.paused = False

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
        try:
            if PAUSED_FILE.exists():
                PAUSED_FILE.unlink()
        except Exception:
            pass


bot_state = BotState()
