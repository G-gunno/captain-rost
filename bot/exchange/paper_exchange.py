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
        self.funding = 0.0
        self.positions = {}
        self.orders = []
        self.trades = []
        self.realized = []
        self._load()

    def _load(self):
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())
                self.usdt = data.get("usdt", self.start_usdt)
                self.funding = data.get("funding", 0.0)
                self.positions = data.get("positions", {})
                self.orders = data.get("orders", [])
                self.trades = data.get("trades", [])
                self.realized = data.get("realized", [])
                logger.info(f"Paper state загружен: USDT={self.usdt:.2f}")
        except Exception as e:
            logger.error(f"Paper load error: {e}")

    def save(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "usdt": self.usdt, "funding": self.funding,
                "positions": self.positions, "orders": self.orders,
                "trades": self.trades, "realized": self.realized,
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Paper save error: {e}")

    def place_limit_buy(self, symbol, qty, price, tp, sl):
        order = {"id": f"paper-{int(time.time() * 1000)}", "side": "Buy",
                 "symbol": symbol, "qty": qty, "price": price,
                 "tp": tp, "sl": sl, "created": int(time.time())}
        self.orders.append(order)
        self.save()
        return order

    def cancel_order(self, order_id):
        self.orders = [o for o in self.orders if o["id"] != order_id]
        self.save()

    def check_fills(self, prices):
        fills = []
        for order in list(self.orders):
            last = prices.get(order["symbol"], {}).get("last")
            if last is None:
                continue
            if order["side"] == "Buy" and last <= order["price"]:
                cost = order["qty"] * order["price"]
                if cost <= self.usdt:
                    self.usdt -= cost
                    pos = self.positions.setdefault(order["symbol"], {"qty": 0.0, "avg": 0.0})
                    total = pos["qty"] + order["qty"]
                    pos["avg"] = (pos["avg"] * pos["qty"] + cost) / total
                    pos["qty"] = total
                    pos["tp"] = order["tp"]
                    pos["sl"] = order["sl"]
                    pos["max_sl"] = order["sl"]
                    pos["entry_time"] = int(time.time())
                    self.trades.append({"side": "Buy", "symbol": order["symbol"],
                                        "qty": order["qty"], "price": order["price"],
                                        "time": int(time.time())})
                    self.orders.remove(order)
                    fills.append(order)
                    logger.info(f"PAPER FILL BUY {order['symbol']} @ {order['price']}")
        self.save()
        return fills

    def check_exits(self, prices):
        results = []
        for sym in list(self.positions):
            last = prices.get(sym, {}).get("last")
            if last is None:
                continue
            pos = self.positions[sym]
            if last >= pos["tp"]:
                results.append(self._sell(sym, pos["tp"], "TP ✅"))
            elif last <= pos["sl"]:
                results.append(self._sell(sym, pos["sl"], "SL 🛡"))
        return results

    def _sell(self, sym, price, reason):
        pos = self.positions.pop(sym)
        proceeds = pos["qty"] * price
        cost = pos["qty"] * pos["avg"]
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100) if cost else 0.0
        self.usdt += proceeds
        transferred = 0.0
        if pnl > 0:
            transferred = round(pnl * 0.62, 4)   # 60-65% прибыли -> Funding
            self.usdt -= transferred
            self.funding += transferred
        self.realized.append({"symbol": sym, "pnl": round(pnl, 4),
                              "pnl_pct": round(pnl_pct, 2), "reason": reason,
                              "time": int(time.time())})
        self.trades.append({"side": "Sell", "symbol": sym, "qty": pos["qty"],
                            "price": price, "time": int(time.time())})
        self.save()
        return {"symbol": sym, "price": price, "pnl": pnl,
                "pnl_pct": pnl_pct, "reason": reason, "transferred": transferred}

    def equity(self, prices):
        eq = self.usdt + self.funding
        for sym, pos in self.positions.items():
            eq += pos["qty"] * prices.get(sym, {}).get("last", 0)
        return eq


paper = PaperExchange()
