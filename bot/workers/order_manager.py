import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper
from bot.strategy.sizing import buy_size, portfolio_limits, tier_limits
from bot.strategy.scanner import threshold
from bot.strategy.learner import learner
from bot.core.orchestrator import entry_offset, pair_html, corr_txt, funding_line, usd, fmt_price, fmt_pct

class OrderManagerWorker:
    """Управляет портфелем: сайзинг, ротация слабейших, выставление ордеров."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.signal_queue = self.bus.subscribe("SIGNALS_READY", maxsize=2)
        self._fomo_cooldowns = {}

    async def run(self):
        logger.info("💼 Order Manager запущен: контроль портфеля и ротация...")
        while True:
            try:
                event = await self.signal_queue.get()
                await self._process_signals(event.payload)
                self.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"OrderManager error: {e}")

    def _notify(self, text: str, urgent: bool = False):
        """Хелпер для быстрой отправки в шину NotificationWorker"""
        self.bus.publish("NOTIFY", {"text": text, "urgent": urgent})

    async def _process_signals(self, payload: dict):
        candidates = payload["candidates"]
        tickers = payload["tickers"]
        regime = payload["regime"]
        
        equity = paper.equity(tickers)
        thr = threshold(regime)
        sec_lim, other_lim = portfolio_limits(equity)
        sat_limit = learner.satellite_limit()
        base_min, _ = tier_limits(equity)
        
        current_time = int(time.time())
        # Очистка кулдаунов
        for k in list(self._fomo_cooldowns.keys()):
            if self._fomo_cooldowns[k] < current_time:
                del self._fomo_cooldowns[k]

        # Перебор найденных сканером кандидатов
        for cand in candidates:
            sym = cand["symbol"]

            if self._fomo_cooldowns.get(sym, 0) > current_time:
                continue
            if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                continue

            kind = cand.get("kind", "core")
            sector = cand.get("sector", "Other")
            entry_mode = cand.get("entry_mode", "rocket" if cand.get("is_momentum") else "sniper")
            is_mom = cand.get("is_momentum", False)
            off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], entry_mode)

            t_data = tickers.get(sym, {})
            bid1 = t_data.get("bid1", cand["last"])
            ideal_entry = cand["last"] * (1 + off)
            entry = ideal_entry if is_mom else min(ideal_entry, bid1)
            
            a = cand["atr"]
            if a <= 0: continue

            # Расчет стопов и тейков (из твоей логики)
            if kind == "satellite":
                base_sl_mult = 0.75 if is_mom else 1.5
                sl_dist_pct = max(min(base_sl_mult * a / entry * 100 * learner.weight("sl_mult"), 5.0), 2.0)
                tp_dist_pct = max(min(2.5 * a / entry * 100 * learner.weight("tp_mult"), 12.0), sl_dist_pct * 2.0)
                sl = entry * (1 - sl_dist_pct / 100)
                tp = entry * (1 + tp_dist_pct / 100)
                min_rr = 2.0
            else:
                sl_dist_atr = 0.6 * a if is_mom else 1.2 * a
                sl_dist_raw = sl_dist_atr * learner.weight("sl_mult")
                tp_dist_raw = max(2.0 * a * learner.weight("tp_mult"), sl_dist_raw * 1.5)
                sl = entry - sl_dist_raw
                tp = max(entry + tp_dist_raw, entry * 1.006)
                sl = min(sl, entry * 0.9965)
                min_rr = 1.5
                if (entry - sl) / entry * 100 > 3.0:
                    continue

            rr = (tp - entry) / (entry - sl) if entry > sl else 0
            if tp <= entry or sl >= entry or round(rr, 2) < min_rr:
                continue

            # Сайзинг
            size = buy_size(equity, cand["score"], thr, cand["liquidity"], paper.usdt,
                            kind=kind, entry_mode=entry_mode, size_multiplier=cand.get("size_mult", 1.0))

            if size < 10: continue

            # Проверка лимитов сателлитов
            if kind == "satellite":
                sat_exp = sum(p["qty"] * tickers.get(s, {}).get("last", 0) for s, p in paper.positions.items() if p.get("kind") == "satellite")
                sat_exp += sum(o["qty"] * o["price"] for o in paper.orders if o.get("kind") == "satellite")
                if sat_exp >= equity * sat_limit / 100:
                    continue

            # === ЛОГИКА РОТАЦИИ ПОРТФЕЛЯ ===
            planned_sells, planned_cancels = [], []
            lim = other_lim if sector == "Other" else sec_lim
            sector_count = sum(1 for p in paper.positions.values() if (p.get("sector") or "Other") == sector)

            if sector_count >= lim:
                if is_mom: 
                    sector_positions = [(s, p) for s, p in paper.positions.items() if (p.get("sector") or "Other") == sector]
                    if sector_positions:
                        w_sym, w_pos = min(sector_positions, key=lambda kv: kv[1].get("score", 0))
                        t_w = tickers.get(w_sym)
                        if t_w:
                            weak_pnl = (t_w["last"] - w_pos["avg"]) / w_pos["avg"] * 100
                            can_rotate = (cand["score"] >= w_pos.get("score", 0) + 1.0 and not w_pos.get("tp1_done") and weak_pnl >= -2.0)
                            if can_rotate:
                                planned_sells.append((w_sym, w_pos, t_w["last"], weak_pnl, "РОТАЦИЯ СЕКТОРА 🔄"))
                            else:
                                continue
                else:
                    continue

            proj_pending = sum(o["qty"] * o["price"] for o in paper.orders)
            proj_usdt = paper.usdt + sum(w_pos["qty"] * last for _, w_pos, last, _, _ in planned_sells)

            if proj_pending + size > proj_usdt:
                # Отмена слабых ордеров
                available_orders = [o for o in paper.orders if o not in planned_cancels]
                if available_orders:
                    w_o = min(available_orders, key=lambda o: o.get("score", 0))
                    if cand["score"] >= w_o.get("score", 0) + 1.0:
                        planned_cancels.append(w_o)
                        proj_pending -= w_o["qty"] * w_o["price"]

                # Продажа слабых позиций
                if is_mom and (proj_pending + size > proj_usdt):
                    planned_syms = [s[0] for s in planned_sells]
                    available_pos = [(s, p) for s, p in paper.positions.items() if s not in planned_syms]
                    if available_pos:
                        w_sym, w_p = min(available_pos, key=lambda kv: kv[1].get("score", 0))
                        t_w = tickers.get(w_sym)
                        if t_w:
                            pnl = (t_w["last"] - w_p["avg"]) / w_p["avg"] * 100
                            if cand["score"] >= w_p.get("score", 0) + 1.5 and not w_p.get("tp1_done") and pnl >= -2.0:
                                planned_sells.append((w_sym, w_p, t_w["last"], pnl, "ОБЩАЯ РОТАЦИЯ 🔄"))
                                proj_usdt += w_p["qty"] * t_w["last"]

            if proj_pending + size > proj_usdt:
                continue # Денег всё равно нет

            # Выполнение ротации
            for w_o in planned_cancels:
                paper.cancel_order(w_o["id"])
                self._notify(f"🔄 <b>Ротация ордера</b> · <b>{w_o['symbol'][:-4]}</b> снят\nМесто для <b>{sym[:-4]}</b>")
                
            for w_sym, w_pos, last, pnl, reason in planned_sells:
                ex = paper._sell(w_sym, last, reason, regime_now=regime)
                self._notify(f"🔄 <b>{reason}</b> · <b>{w_sym[:-4]}</b> → <b>{sym[:-4]}</b>\n💵 Освобождено {usd(ex['pnl'])}")

            # Выставление ордера
            qty = size / entry
            order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl, score=cand["score"], reason_keys=cand.get("reason_keys", []))
            order.update({"kind": kind, "sector": sector, "tier": cand.get("tier"), "corr": cand.get("corr"), "regime": regime, "is_momentum": is_mom, "entry_mode": entry_mode})
            paper.save()
            paper.log_event(sym, "order_placed", entry, mode=entry_mode)

            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            new_tag = "· 🆕 " if cand.get("is_new") else ""
            
            self._notify(
                f"📋 <b>Ордер</b> {new_tag}· {pair_html(sym[:-4], order)}\n"
                f"💵 {usd(size)} · 📥 {fmt_price(entry)} ({off * 100:+.2f}%){corr_txt(cand)}\n"
                f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
                f"⭐ {cand['score']:.1f} · 🧠 {'; '.join(cand['reasons'][:3])}"
            )
