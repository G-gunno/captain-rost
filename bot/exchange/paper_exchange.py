import os
import json
import time
from pathlib import Path

from loguru import logger

from bot.strategy.learner import learner
from bot.core.remote_state import download_state, upload_state

REMOTE_PATH = "paper_state.json"


class PaperExchange:
    """Виртуальная биржа с резервированием состояния в GitHub."""

    FEE_PCT = 0.10

    def __init__(self, start_usdt: float = 1000.0):
        self.state_file = Path(os.getenv("STORAGE_DIR", "storage")) / "paper_state.json"
        self.start_usdt = start_usdt
        self.usdt = start_usdt
        self.funding = 0.0
        self.positions = {}
        self.orders = []
        self.trades = []
        self.realized = []
        self._last_upload = 0.0
        self._load()
        self._migrate_sectors()

    def _load(self):
        data = None
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())
        except Exception as e:
            logger.error(f"Paper load error: {e}")
        if data is None:
            data = download_state(REMOTE_PATH)
            if data:
                logger.info("Paper state восстановлен из GitHub (пережил деплой)")
        if data:
            self.usdt = data.get("usdt", self.start_usdt)
            self.funding = data.get("funding", 0.0)
            self.positions = data.get("positions", {})
            self.orders = data.get("orders", [])
            self.trades = data.get("trades", [])
            self.realized = data.get("realized", [])
            logger.info(f"Paper state загружен: USDT={self.usdt:.2f}, позиций={len(self.positions)}")

    def _migrate_sectors(self):
        try:
            from bot.news.cmc import sector_of
        except ImportError:
            return
        migrated = 0
        for sym, pos in self.positions.items():
            base = sym[:-4] if sym.endswith("USDT") else sym
            new_sector = sector_of(base)
            if new_sector and new_sector != pos.get("sector"):
                pos["sector"] = new_sector
                migrated += 1
        for order in self.orders:
            base = order["symbol"][:-4] if order["symbol"].endswith("USDT") else order["symbol"]
            new_sector = sector_of(base)
            if new_sector and new_sector != order.get("sector"):
                order["sector"] = new_sector
                migrated += 1
        if migrated:
            logger.info(f"Миграция секторов: переопределено {migrated} позиций/ордеров")
            self.save()
        else:
            logger.info("Миграция секторов: все секторы актуальны")

    def save(self):
        payload = {
            "usdt": self.usdt,
            "funding": self.funding,
            "positions": self.positions,
            "orders": self.orders,
            "trades": self.trades,
            "realized": self.realized,
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Paper save error: {e}")
        if time.time() - self._last_upload > 60:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def _resolve_sector(self, symbol, fallback_sector=None):
        if fallback_sector and fallback_sector != "Other":
            return fallback_sector
        try:
            from bot.news.cmc import sector_of
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            return sector_of(base) or "Other"
        except Exception:
            return "Other"

    def place_limit_buy(self, symbol, qty, price, tp, sl, score=0, reason_keys=None):
        order = {
            "id": f"paper-{int(time.time() * 1000)}",
            "side": "Buy",
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "tp": tp,
            "sl": sl,
            "score": score,
            "reason_keys": reason_keys or [],
            "requotes": 0,
            "created": int(time.time()),
        }
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
                fee = cost * self.FEE_PCT / 100
                if cost + fee <= self.usdt:
                    self.usdt -= cost + fee
                    pos = self.positions.setdefault(order["symbol"], {"qty": 0.0, "avg": 0.0})
                    total = pos["qty"] + order["qty"]
                    pos["avg"] = (pos["avg"] * pos["qty"] + cost) / total
                    pos["qty"] = total
                    pos["tp"] = order["tp"]
                    pos["sl"] = order["sl"]
                    pos["max_sl"] = order["sl"]
                    pos["score"] = order.get("score", 0)
                    pos["reason_keys"] = order.get("reason_keys", [])
                    pos["kind"] = order.get("kind", "core")
                    pos["sector"] = self._resolve_sector(order["symbol"], order.get("sector"))
                    pos["max_price"] = order["price"]
                    pos["entry_time"] = int(time.time())
                    self.trades.append({
                        "side": "Buy",
                        "symbol": order["symbol"],
                        "qty": order["qty"],
                        "price": order["price"],
                        "time": int(time.time()),
                    })
                    self.orders.remove(order)
                    fills.append(order)
                    logger.info(f"PAPER FILL BUY {order['symbol']} {order['qty']} @ {order['price']}")
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

    def sell_partial(self, sym, qty_part, price, reason):
        """Частичный выход TP1: помечается partial=True — не входит в счётчик сделок."""
        pos = self.positions.get(sym)
        if not pos:
            return None
        qty_part = min(qty_part, pos["qty"])
        proceeds = qty_part * price
        cost_part = qty_part * pos["avg"]
        fee_buy = cost_part * self.FEE_PCT / 100
        fee_sell = proceeds * self.FEE_PCT / 100
        pnl = (proceeds - fee_sell) - (cost_part + fee_buy)
        pnl_pct = pnl / (cost_part + fee_buy) * 100 if cost_part else 0.0

        pos["partial_pnl"] = pos.get("partial_pnl", 0.0) + pnl
        pos["partial_cost"] = pos.get("partial_cost", 0.0) + (cost_part + fee_buy)
        pos["tp1_price"] = price
        pos["max_price"] = max(pos.get("max_price", 0.0), price)

        self.usdt += proceeds - fee_sell
        transferred = 0.0
        if pnl > 0:
            transferred = round(pnl * 0.62, 4)
            self.usdt -= transferred
            self.funding += transferred
        pos["qty"] -= qty_part
        self.realized.append({
            "symbol": sym, "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 2),
            "reason": reason, "time": int(time.time()), "partial": True,
        })
        self.trades.append({
            "side": "Sell(part)", "symbol": sym, "qty": qty_part,
            "price": price, "time": int(time.time()),
        })
        self.save()
        return {
            "symbol": sym, "price": price, "pnl": pnl,
            "pnl_pct": pnl_pct, "reason": reason, "transferred": transferred,
        }

    def _sell(self, sym, price, reason):
        """Финальный выход: одна запись learner + позиция целиком в realized."""
        pos = self.positions.pop(sym)
        proceeds = pos["qty"] * price
        cost = pos["qty"] * pos["avg"]
        fee_buy = cost * self.FEE_PCT / 100
        fee_sell = proceeds * self.FEE_PCT / 100
        pnl_final = (proceeds - fee_sell) - (cost + fee_buy)
        pnl_final_pct = pnl_final / (cost + fee_buy) * 100 if (cost + fee_buy) else 0.0

        partial_pnl = pos.get("partial_pnl", 0.0)
        partial_cost = pos.get("partial_cost", 0.0)
        total_pnl = partial_pnl + pnl_final
        total_cost = partial_cost + (cost + fee_buy)
        total_pnl_pct = total_pnl / total_cost * 100 if total_cost else pnl_final_pct

        if reason.startswith("TP"):
            exit_type = "TP1_RUN" if pos.get("tp1_done") else "TP"
        elif reason.startswith("SL"):
            exit_type = "TP1_SL" if pos.get("tp1_done") else "SL"
        else:
            exit_type = "EARLY"

        runner_bonus = 0.0
        tp1_price = pos.get("tp1_price")
        max_price = pos.get("max_price", 0.0)
        if tp1_price and max_price > tp1_price:
            runner_bonus = (max_price - tp1_price) / tp1_price * 100

        sector = self._resolve_sector(sym, pos.get("sector"))
        try:
            learner.record(pos.get("reason_keys", []), total_pnl > 0,
                           total_pnl_pct, sector=sector,
                           exit_type=exit_type, runner_bonus=runner_bonus)
        except Exception as e:
            logger.error(f"learner record error: {e}")

        self.usdt += proceeds - fee_sell
        transferred = 0.0
        if pnl_final > 0:
            transferred = round(pnl_final * 0.62, 4)
            self.usdt -= transferred
            self.funding += transferred
        # В realized — результат ПОЗИЦИИ ЦЕЛИКОМ (TP1 + остаток)
        self.realized.append({
            "symbol": sym, "pnl": round(total_pnl, 4), "pnl_pct": round(total_pnl_pct, 2),
            "reason": reason, "time": int(time.time()), "exit_type": exit_type,
        })
        self.trades.append({
            "side": "Sell", "symbol": sym, "qty": pos["qty"],
            "price": price, "time": int(time.time()),
        })
        self.save()
        return {
            "symbol": sym, "price": price, "pnl": total_pnl,
            "pnl_pct": total_pnl_pct, "reason": reason, "transferred": transferred,
            "exit_type": exit_type, "runner_bonus": runner_bonus,
        }

    def sell_all(self, prices):
        results = []
        for sym in list(self.positions):
            last = prices.get(sym, {}).get("last")
            if last:
                results.append(self._sell(sym, last, "EXITALL 🛑"))
        self.orders = []
        self.save()
        return results

    def reset_stats(self):
        self.realized = []
        self.trades = []
        self.save()
        logger.info("paper: торговая статистика сброшена")

    def equity(self, prices):
        eq = self.usdt + self.funding
        for sym, pos in self.positions.items():
            eq += pos["qty"] * prices.get(sym, {}).get("last", 0)
        return eq

    def get_metrics(self, prices=None):
        """Метрики по ЗАКРЫТЫМ ПОЗИЦИЯМ (частичные TP1 не входят в счётчик сделок)."""
        if prices is None:
            prices = {}
        finals = [r for r in self.realized if not r.get("partial")]
        partial_count = len(self.realized) - len(finals)

        wins = [r for r in finals if r["pnl"] > 0]
        losses = [r for r in finals if r["pnl"] <= 0]
        sum_win = sum(r["pnl"] for r in wins)
        sum_loss = abs(sum(r["pnl"] for r in losses))
        if sum_loss == 0:
            profit_factor = None if sum_win == 0 else float("inf")
        else:
            profit_factor = sum_win / sum_loss

        eq = self.start_usdt
        peak = eq
        max_dd = 0.0
        for r in finals:
            eq += r["pnl"]
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100
                if dd > max_dd:
                    max_dd = dd

        total_trades = len(finals)
        if total_trades > 0:
            avg_win = sum_win / len(wins) if wins else 0
            avg_loss = sum_loss / len(losses) if losses else 0
            winrate = len(wins) / total_trades
            lossrate = 1 - winrate
            expectancy = (winrate * avg_win) - (lossrate * avg_loss)
        else:
            expectancy = 0

        total_pnl = sum(r["pnl"] for r in finals)
        recovery_factor = total_pnl / max_dd if max_dd > 0 else 0

        wr, n = learner.winrate()
        return {
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_dd,
            "winrate": wr,
            "win_count": len(wins),
            "loss_count": len(losses),
            "total_trades": total_trades,
            "partial_count": partial_count,
            "total_pnl": total_pnl,
            "expectancy": expectancy,
            "recovery_factor": recovery_factor,
        }


paper = PaperExchange()
