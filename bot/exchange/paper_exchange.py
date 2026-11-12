import os
import json
import time
from pathlib import Path
from collections import defaultdict

from loguru import logger

from bot.strategy.learner import learner, is_neutral_be
from bot.strategy.sizing import MAX_POSITIONS_PER_TIER
from bot.core.remote_state import download_state, upload_state

REMOTE_PATH = "paper_state.json"
MAX_EVENTS_PER_SYMBOL = 150
MAX_TOTAL_EVENTS = 5000


class PaperExchange:
    """Виртуальная биржа с Умным Овербукингом, защитой от каскадного залива (Velocity Shield) и 48ч аналитикой."""

    FEE_PCT = 0.10

    def __init__(self, start_usdt: float = 1000.0):
        self.state_file = Path(os.getenv("STORAGE_DIR", "storage")) / "paper_state.json"
        self.start_usdt = start_usdt
        self.usdt = start_usdt
        self.funding = 0.0
        self.positions = {}
        self.orders = []
        self.trades = []
        self.funding_coins = {}
        self.realized = []
        self.market_history = []
        self.chart_events = []
        self._last_upload = 0.0
        self._btc_window = []
        self._recent_fill_ts = []
        self._last_velocity_log_ts = 0.0
        self._auto_cancelled = []
        self._load()

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
            self.usdt = data.get("usdt", self.start_usdt)
            self.funding = data.get("funding", 0.0)
            self.positions = data.get("positions", {})
            self.orders = data.get("orders", [])
            self.trades = data.get("trades", [])
            self.funding_coins = data.get("funding_coins", {})
            self.realized = data.get("realized", [])
            self.market_history = data.get("market_history", [])
            self.chart_events = data.get("chart_events", [])
            if not getattr(learner, "coin_stats", None):
                for r in self.realized:
                    if not r.get("partial") and r.get("symbol"):
                        learner.coin_stats.setdefault(r["symbol"], []).append(round(float(r.get("pnl_pct", 0.0)), 2))
                learner.save()
            logger.info(f"Paper state загружен: USDT={self.usdt:.2f}, позиций={len(self.positions)}")
        else:
            self.chart_events = []

    def _prune_chart_events(self) -> list:
        if not hasattr(self, "chart_events") or not self.chart_events:
            return []
        by_sym = defaultdict(list)
        for ev in self.chart_events:
            by_sym[ev.get("sym", "UNKNOWN")].append(ev)

        pruned = []
        for sym, ev_list in by_sym.items():
            pruned.extend(ev_list[-MAX_EVENTS_PER_SYMBOL:])

        pruned.sort(key=lambda x: x.get("ts", 0))
        self.chart_events = pruned[-MAX_TOTAL_EVENTS:]
        return self.chart_events

    def save(self):
        payload = {
            "usdt": self.usdt,
            "funding": self.funding,
            "positions": self.positions,
            "orders": self.orders,
            "trades": self.trades,
            "funding_coins": getattr(self, "funding_coins", {}),
            "realized": self.realized,
            "market_history": self.market_history,
            "chart_events": self._prune_chart_events(),
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error(f"Paper save error: {e}")
        if time.time() - self._last_upload > 60:
            self._last_upload = time.time()
            upload_state(REMOTE_PATH, payload)

    def pop_auto_cancelled(self) -> list:
        items = list(self._auto_cancelled)
        self._auto_cancelled.clear()
        return items

    def log_event(self, symbol, ev_type, price, text="", rsi_val=None, mode=None, tp=None, sl=None, sup=None, res=None):
        if not hasattr(self, "chart_events"):
            self.chart_events = []
        self.chart_events.append({
            "ts": int(time.time()),
            "sym": symbol,
            "type": ev_type,
            "price": price,
            "text": text,
            "rsi": round(rsi_val, 1) if rsi_val else None,
            "mode": mode,
            "tp": round(float(tp), 8) if tp is not None else None,
            "sl": round(float(sl), 8) if sl is not None else None,
            "sup": round(float(sup), 8) if sup is not None else None,
            "res": round(float(res), 8) if res is not None else None,
        })

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
        now_ts = int(time.time())
        order = {
            "id": f"paper-{int(time.time() * 1000)}",
            "side": "Buy",
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "tp": tp,
            "sl": sl,
            "score": score,
            "initial_score": score,
            "reason_keys": reason_keys or [],
            "requotes": 0,
            "created": now_ts,
            "last_move_ts": now_ts,
        }
        self.orders.append(order)
        self.save()
        return order

    def cancel_order(self, order_id):
        self.orders = [o for o in self.orders if o["id"] != order_id]
        self.save()

    def count_tier_positions(self, tier: str) -> int:
        t_norm = tier or "SMALL"
        return sum(1 for p in self.positions.values() if (p.get("tier") or "SMALL") == t_norm)

    def count_tier_orders(self, tier: str) -> int:
        t_norm = tier or "SMALL"
        return sum(1 for o in self.orders if (o.get("tier") or "SMALL") == t_norm)

    def effective_locked_usdt(self) -> float:
        """
        Умный Овербукинг кэша:
        Поскольку в каждом тире может исполниться не более (MAX_POSITIONS_PER_TIER - открыто_в_тире) ордеров,
        а остальные резервные ордера тира автоматически снимутся, считаем заблокированным только
        реально исполнимый объём топ-ордеров каждого тира.
        """
        if not self.orders:
            return 0.0
        orders_by_tier = defaultdict(list)
        for o in self.orders:
            t = o.get("tier") or "SMALL"
            cost = o["qty"] * o["price"] * (1.0 + self.FEE_PCT / 100.0)
            orders_by_tier[t].append(cost)

        total_locked = 0.0
        for t, costs in orders_by_tier.items():
            avail_slots = max(0, MAX_POSITIONS_PER_TIER - self.count_tier_positions(t))
            if avail_slots > 0:
                costs.sort(reverse=True)
                total_locked += sum(costs[:avail_slots])
        return round(total_locked, 2)

    def check_fills(self, prices):
        now_ts = time.time()
        btc_last = prices.get("BTCUSDT", {}).get("last", 0.0)
        is_btc_dumping = False
        if btc_last > 0:
            self._btc_window.append((now_ts, btc_last))
            self._btc_window = [x for x in self._btc_window if now_ts - x[0] <= 300]
            if len(self._btc_window) >= 2:
                max_btc_5m = max(x[1] for x in self._btc_window)
                if max_btc_5m > 0 and (btc_last - max_btc_5m) / max_btc_5m <= -0.0035:
                    is_btc_dumping = True

        self._recent_fill_ts = [ts for ts in self._recent_fill_ts if now_ts - ts <= 180]

        fills = []
        sorted_orders = sorted(
            list(self.orders),
            key=lambda o: (1 if o.get("grade") == "A+" else 0, float(o.get("score", 0.0))),
            reverse=True,
        )

        for order in sorted_orders:
            if order not in self.orders:
                continue
            sym = order["symbol"]
            last = prices.get(sym, {}).get("last")
            if last is None:
                continue

            if order["side"] == "Buy" and last <= order["price"]:
                tier = order.get("tier") or "SMALL"
                if sym not in self.positions and self.count_tier_positions(tier) >= MAX_POSITIONS_PER_TIER:
                    self.orders.remove(order)
                    self._auto_cancelled.append(order)
                    self.log_event(
                        sym, "cancel", last,
                        f"Лимит позиций тира {tier} ({MAX_POSITIONS_PER_TIER}/{MAX_POSITIONS_PER_TIER})",
                        tp=order.get("tp"), sl=order.get("sl"),
                        sup=order.get("nearest_sup"), res=order.get("nearest_res")
                    )
                    logger.info(f"PAPER CANCEL {sym}: тир {tier} уже заполнен ({MAX_POSITIONS_PER_TIER} откр. поз.)")
                    continue

                if is_btc_dumping and len(self._recent_fill_ts) >= 2:
                    if now_ts - self._last_velocity_log_ts >= 60:
                        self._last_velocity_log_ts = now_ts
                        logger.info(f"🛡 Velocity Shield: {sym} придержан (уже 2 покупки за 3 мин на проливе BTC)")
                    continue

                cost = order["qty"] * order["price"]
                fee = cost * self.FEE_PCT / 100
                if cost + fee <= self.usdt:
                    self.usdt -= cost + fee
                    self._recent_fill_ts.append(now_ts)
                    pos = self.positions.setdefault(sym, {"qty": 0.0, "avg": 0.0})
                    total = pos["qty"] + order["qty"]
                    pos["avg"] = (pos["avg"] * pos["qty"] + cost) / total
                    pos["qty"] = total
                    pos["tp"] = order["tp"]
                    pos["sl"] = order["sl"]
                    pos["initial_tp"] = order["tp"]
                    pos["initial_sl"] = order["sl"]
                    pos["max_sl"] = order["sl"]
                    pos["score"] = order.get("score", 0)
                    pos["thr"] = order.get("thr", 0)
                    pos["risk_mode"] = order.get("risk_mode", getattr(learner, "current_risk_mode", "NORMAL"))
                    pos["gate_1h"] = order.get("gate_1h", "1H_TREND_OK")
                    pos["reason_1h"] = order.get("reason_1h", "")
                    pos["chg_48h"] = order.get("chg_48h", 0.0)
                    pos["pos_in_48h"] = order.get("pos_in_48h", 0.5)
                    pos["corr"] = order.get("corr")
                    pos["reasons"] = order.get("reasons", [])
                    pos["reason_keys"] = order.get("reason_keys", [])
                    pos["patterns"] = order.get("patterns", [])
                    pos["grade"] = order.get("grade", "B")
                    pos["sector"] = self._resolve_sector(sym, order.get("sector"))
                    pos["tier"] = tier
                    pos["regime_entry"] = order.get("regime")
                    pos["is_momentum"] = order.get("is_momentum", False)
                    pos["entry_mode"] = order.get("entry_mode", "sniper")
                    pos["max_price"] = order["price"]
                    pos["order_created"] = order.get("created", int(time.time()))
                    pos["entry_time"] = int(time.time())
                    pos["nearest_sup"] = order.get("nearest_sup")
                    pos["nearest_res"] = order.get("nearest_res")
                    pos["sup_touches"] = order.get("sup_touches", 0)
                    pos["res_touches"] = order.get("res_touches", 0)
                    pos["sl_state"] = order.get("sl_state", "SHIELD")
                    pos["_last_sl_notify_ts"] = 0
                    pos["_ema21"] = order.get("_ema21", 0.0)
                    pos["_ema50"] = order.get("_ema50", 0.0)
                    pos["_atr15"] = order.get("_atr15", 0.0)
                    self.trades.append({
                        "side": "Buy",
                        "symbol": sym,
                        "qty": order["qty"],
                        "price": order["price"],
                        "time": int(time.time()),
                    })
                    self.orders.remove(order)
                    fills.append(order)
                    logger.info(f"PAPER FILL BUY {sym} {order['qty']} @ {order['price']} (tier={tier})")

                    if self.count_tier_positions(tier) >= MAX_POSITIONS_PER_TIER:
                        for other_o in list(self.orders):
                            if (other_o.get("tier") or "SMALL") == tier:
                                o_sym = other_o["symbol"]
                                o_last = prices.get(o_sym, {}).get("last", other_o["price"])
                                self.orders.remove(other_o)
                                self._auto_cancelled.append(other_o)
                                self.log_event(
                                    o_sym, "cancel", o_last,
                                    f"Наполнен лимит 3 поз. тира {tier}",
                                    tp=other_o.get("tp"), sl=other_o.get("sl"),
                                    sup=other_o.get("nearest_sup"), res=other_o.get("nearest_res")
                                )
                                logger.info(f"PAPER AUTO-CANCEL {o_sym}: тир {tier} наполнен до {MAX_POSITIONS_PER_TIER} позиций")
        self.save()
        return fills

    def check_exits(self, prices, regime_now=None):
        results = []
        for sym in list(self.positions):
            last = prices.get(sym, {}).get("last")
            if last is None:
                continue
            pos = self.positions[sym]
            if last >= pos["tp"]:
                results.append(self._sell(sym, pos["tp"], "TP🏃🏻‍➡️", regime_now=regime_now))
            elif last <= pos["sl"]:
                results.append(self._sell(sym, pos["sl"], "SL", regime_now=regime_now))
        return results

    def sell_partial(self, sym, qty_part, price, reason):
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
            hodl_reserve_usdt = round(pnl * 0.30, 4)
            tier = pos.get("tier")
            if tier == "TOP20" and price > 0:
                qty_kept = min(qty_part * 0.5, hodl_reserve_usdt / price)
                if qty_kept > 0:
                    actual_reserved_usdt = round(qty_kept * price, 4)
                    self.usdt -= actual_reserved_usdt
                    if not hasattr(self, "funding_coins"):
                        self.funding_coins = {}
                    self.funding_coins[sym] = self.funding_coins.get(sym, 0.0) + qty_kept
                    reason += " 🏦"
            else:
                transferred = hodl_reserve_usdt
                self.usdt -= transferred
                self.funding += transferred

        sector = self._resolve_sector(sym, pos.get("sector"))
        tier = pos.get("tier")
        entry_mode = pos.get("entry_mode", "rocket" if pos.get("is_momentum") else "sniper")

        pos["qty"] -= qty_part
        self.realized.append({
            "symbol": sym,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "time": int(time.time()),
            "exit_type": "🔥 TP1",
            "sector": sector,
            "tier": tier,
            "grade": pos.get("grade", "B"),
            "is_momentum": pos.get("is_momentum", False),
            "entry_mode": entry_mode,
            "regime": pos.get("regime_entry", "neutral"),
            "partial": True,
        })
        self.trades.append({
            "side": "Sell(part)",
            "symbol": sym,
            "qty": qty_part,
            "price": price,
            "time": int(time.time()),
        })
        self.save()
        return {
            "symbol": sym, "price": price, "pnl": pnl,
            "pnl_pct": pnl_pct, "reason": reason, "transferred": transferred,
            "sector": sector, "tier": tier, "grade": pos.get("grade", "B"),
            "is_momentum": pos.get("is_momentum", False), "entry_mode": entry_mode,
        }

    def _sell(self, sym, price, reason, regime_now=None):
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

        sl_state = pos.get("sl_state", "SHIELD")
        is_be = is_neutral_be(total_pnl_pct)
        tp1_price = pos.get("tp1_price") or pos.get("initial_tp")

        if "TP" in reason:
            exit_type = "TP🏃🏻‍➡️" if pos.get("tp1_done") else "🔥 TP1"
        elif "SL" in reason or "🛡" in reason:
            if is_be:
                exit_type = "🛡🔒"
            elif sl_state == "ABOVE_TP1" or (pos.get("tp1_done") and tp1_price and price >= tp1_price * 0.9995):
                exit_type = "🛡🔥"
            elif sl_state == "TRAILING_RUNNER" or (pos.get("tp1_done") and total_pnl > 0):
                exit_type = "🛡📈" if sl_state == "TRAILING_RUNNER" else "🛡💼"
            elif sl_state in ("PROFIT_LOCK", "BREAKEVEN") and total_pnl_pct > 0.25:
                exit_type = "🛡💼"
            else:
                exit_type = "🛡📉"
        else:
            exit_type = "🛡🔒" if is_be else "EARLY"

        soft = False
        regime_changed = (
            regime_now is not None
            and pos.get("regime_entry")
            and regime_now != pos["regime_entry"]
        )
        if regime_changed and total_pnl < 0 and exit_type in ("🛡📉", "EARLY"):
            soft = True

        runner_bonus = 0.0
        max_price = pos.get("max_price", 0.0)
        if tp1_price and max_price > tp1_price:
            runner_bonus = (max_price - tp1_price) / tp1_price * 100

        sector = self._resolve_sector(sym, pos.get("sector"))
        tier = pos.get("tier")
        entry_mode = pos.get("entry_mode", "rocket" if pos.get("is_momentum") else "sniper")
        grade = pos.get("grade", "B")

        try:
            learner.record(
                pos.get("reason_keys", []), pnl > 0, pnl_pct=total_pnl_pct,
                sector=pos.get("sector"), tier=pos.get("tier"),
                exit_type=exit_type, runner_bonus=runner_bonus, soft=soft,
                entry_mode=pos.get("entry_mode", "sniper"),
                patterns=pos.get("patterns", []),
                grade=pos.get("grade", "B"),
                symbol=sym,
            )
        except Exception as e:
            logger.error(f"learner record error: {e}")

        self.usdt += proceeds - fee_sell
        transferred = 0.0

        if pnl_final > 0 and not is_neutral_be(pnl_final_pct):
            hodl_reserve_usdt = round(pnl_final * 0.30, 4)
            if tier == "TOP20" and price > 0:
                qty_kept = min(pos["qty"] * 0.5, hodl_reserve_usdt / price)
                if qty_kept > 0:
                    actual_reserved_usdt = round(qty_kept * price, 4)
                    self.usdt -= actual_reserved_usdt
                    if not hasattr(self, "funding_coins"):
                        self.funding_coins = {}
                    self.funding_coins[sym] = self.funding_coins.get(sym, 0.0) + qty_kept
                    reason += " 🏦"
            else:
                transferred = hodl_reserve_usdt
                self.usdt -= transferred
                self.funding += transferred

        exit_ts = int(time.time())
        self.realized.append({
            "symbol": sym,
            "pnl": round(total_pnl, 4),
            "pnl_pct": round(total_pnl_pct, 2),
            "reason": reason,
            "time": exit_ts,
            "order_created": pos.get("order_created", pos.get("entry_time", exit_ts)),
            "entry_time": pos.get("entry_time", exit_ts),
            "entry_price": pos.get("avg", price),
            "exit_price": price,
            "initial_tp": pos.get("initial_tp", pos.get("tp")),
            "initial_sl": pos.get("initial_sl", pos.get("sl")),
            "tp1_done": pos.get("tp1_done", False),
            "exit_type": exit_type,
            "sl_state": sl_state,
            "is_be": is_be,
            "grade": grade,
            "score": pos.get("score", 0.0),
            "thr": pos.get("thr", 0.0),
            "risk_mode": pos.get("risk_mode", "NORMAL"),
            "gate_1h": pos.get("gate_1h", ""),
            "reason_1h": pos.get("reason_1h", ""),
            "chg_48h": pos.get("chg_48h", 0.0),
            "pos_in_48h": pos.get("pos_in_48h", 0.5),
            "corr": pos.get("corr"),
            "reasons": pos.get("reasons", []),
            "reason_keys": pos.get("reason_keys", []),
            "sector": sector,
            "tier": tier,
            "is_momentum": pos.get("is_momentum", False),
            "entry_mode": entry_mode,
            "patterns": pos.get("patterns", []),
            "regime": pos.get("regime_entry", "neutral"),
        })
        self.trades.append({
            "side": "Sell", "symbol": sym, "qty": pos["qty"],
            "price": price, "time": exit_ts,
        })
        self.save()
        return {
            "symbol": sym, "price": price, "pnl": total_pnl,
            "pnl_pct": total_pnl_pct, "reason": reason, "transferred": transferred,
            "exit_type": exit_type, "sl_state": sl_state, "is_be": is_be, "grade": grade,
            "runner_bonus": runner_bonus,
            "sector": sector, "tier": tier,
            "is_momentum": pos.get("is_momentum", False),
            "entry_mode": entry_mode,
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
        self.realized.clear()
        self.trades.clear()
        self.market_history.clear()
        self.positions.clear()
        self.orders.clear()
        self.chart_events.clear()
        self._btc_window.clear()
        self._recent_fill_ts.clear()
        self._auto_cancelled.clear()
        if hasattr(self, "funding_coins"):
            self.funding_coins.clear()
        self.usdt = self.start_usdt
        self.funding = 0.0
        self.save()
        logger.info("paper: торговая статистика, позиции, балансы и накопления сброшены")

    def equity(self, prices):
        eq = self.usdt + self.funding
        for sym, pos in self.positions.items():
            eq += pos["qty"] * prices.get(sym, {}).get("last", 0)
        for sym, qty in getattr(self, "funding_coins", {}).items():
            if qty > 0:
                eq += qty * prices.get(sym, {}).get("last", 0)
        return eq

    def get_metrics(self, prices=None, hours=None):
        if prices is None:
            prices = {}

        finals = [r for r in self.realized if not r.get("partial")]

        if hours is not None:
            cutoff = int(time.time()) - int(hours * 3600)
            finals = [r for r in finals if r.get("time", 0) >= cutoff]
            partials = [r for r in self.realized if r.get("partial") and r.get("time", 0) >= cutoff]
            partial_count = len(partials)
        else:
            partial_count = len(self.realized) - len(finals)

        be_trades = [r for r in finals if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
        decisive = [r for r in finals if r not in be_trades]
        wins = [r for r in decisive if r["pnl"] > 0]
        losses = [r for r in decisive if r["pnl"] <= 0]

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

        total_pnl = sum(r["pnl"] for r in finals)
        total_trades = len(finals)
        decisive_count = len(decisive)

        if decisive_count > 0:
            avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0
            avg_loss = abs(sum(r["pnl"] for r in losses)) / len(losses) if losses else 0
            winrate = len(wins) / decisive_count
            lossrate = 1 - winrate
            expectancy = (winrate * avg_win) - (lossrate * avg_loss)

            if max_dd > 0:
                recovery_factor = total_pnl / max_dd
            else:
                recovery_factor = float("inf") if total_pnl > 0 else 0.0
        else:
            expectancy = None
            recovery_factor = None

        wr, _ = learner.winrate()
        return {
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_dd,
            "winrate": wr,
            "win_count": len(wins),
            "loss_count": len(losses),
            "be_count": len(be_trades),
            "total_trades": total_trades,
            "partial_count": partial_count,
            "total_pnl": total_pnl,
            "expectancy": expectancy,
            "recovery_factor": recovery_factor,
        }


paper = PaperExchange()
