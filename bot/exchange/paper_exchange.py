import os
import json
import time
from pathlib import Path

from loguru import logger


class PaperExchange:
    """Виртуальная биржа: реальные цены, виртуальные деньги."""

    def __init__(self, start_usdt: float = 1000.0):
        self.state_file = Path(os.getenv("STORAGE_DIR", "storage")) / "paper_state.json"
        self.start_usdt = start_usdt
        self.usdt = start_usdt
        self.positions = {}  # symbol -> {"qty": float, "avg": float}
        self.orders = []     # активные лимитные ордера
        self.trades = []     # история сделок
        self._load()

    def _load(self):
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())
                self.usdt = data.get("usdt", self.start_usdt)
                self.positions = data.get("positions", {})
                self.orders = data.get("orders", [])
                self.trades = data.get("trades", [])
                logger.info(f"Paper state загружен: USDT={self.usdt:.2f}, позиций={len(self.positions)}")
        except Exception as e:
            logger.error(f"Paper load error: {e}")

    def save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "usdt": self.usdt,
                "positions": self.positions,
                "orders": self.orders,
                "trades": self.trades,
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Paper save error: {e}")

    def place_limit_buy(self, symbol, qty, price):
        order = {"id": f"paper-{int(time.time() * 1000)}", "side": "Buy",
                 "symbol": symbol, "qty": qty, "price": price, "created": int(time.time())}
        self.orders.append(order)
        self.save()
        return order

    def cancel_order(self, order_id):
        self.orders = [o for o in self.orders if o["id"] != order_id]
        self.save()

    def check_fills(self, prices: dict):
        """Лимитный ордер на покупку исполняется, если рынок опустился до цены ордера."""
        for order in list(self.orders):
            last = prices.get(order["symbol"], {}).get("last")
            if last is None:
                continue
            if order["side"] == "Buy" and last <= order["price"]:
                cost = order["qty"] * order["price"]
                if cost <= self.usdt:
                    self.usdt -= cost
                    pos = self.positions.setdefault(order["symbol"], {"qty": 0.0, "avg": 0.0})
                    total_qty = pos["qty"] + order["qty"]
                    pos["avg"] = (pos["avg"] * pos["qty"] + cost) / total_qty
                    pos["qty"] = total_qty
                    self.trades.append({"side": "Buy", "symbol": order["symbol"],
                                        "qty": order["qty"], "price": order["price"],
                                        "time": int(time.time())})
                    self.orders.remove(order)
                    logger.info(f"PAPER FILL BUY {order['symbol']} {order['qty']} @ {order['price']}")
        self.save()

    def equity(self, prices: dict) -> float:
        eq = self.usdt
        for sym, pos in self.positions.items():
            eq += pos["qty"] * prices.get(sym, {}).get("last", 0)
        return eq


paper = PaperExchange()
