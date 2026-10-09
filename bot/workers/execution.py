import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper
from bot.utils.format import pair_html, usd, fmt_price, fmt_pct, pnl_emoji, funding_line
from bot.strategy.shadow import shadow

class ExecutionRiskWorker:
    """Критический воркер ведения открытых позиций (Трейлинг-стопы, TP/SL, Риск-менеджмент). Zero-delay."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED", maxsize=5)
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP", maxsize=1)
        self._last_btc_price = 0.0

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: мониторинг рисков, исполнение ордеров и трейлинг-стопы")
        await asyncio.gather(
            self._price_loop(),
            self._emergency_loop()
        )

    async def _price_loop(self) -> None:
        while True:
            try:
                event = await self.price_queue.get()
                tickers = event.payload
                self._process_tick(tickers)
                self.price_queue.task_done()
            except Exception as e:
                logger.error(f"Execution _price_loop error: {e}")

    async def _emergency_loop(self) -> None:
        while True:
            try:
                event = await self.emergency_queue.get()
                tickers = event.payload
                logger.critical("🚨 Execution Worker: ЭКСТРЕННЫЙ ВЫХОД! Дамп рынка.")
                results = paper.sell_all(tickers)
                
                for ex in results:
                    self.bus.publish("NOTIFY", {"text": f"🚨 <b>Экстренный выход</b> · {ex['symbol']} · {ex['pnl_pct']:.2f}%", "urgent": True})
                
                self.emergency_queue.task_done()
            except Exception as e:
                logger.error(f"Execution _emergency_loop error: {e}")

    def _process_tick(self, tickers: dict) -> None:
        # 1. Проверка глобального риска (Дамп BTC)
        btc = tickers.get("BTCUSDT")
        if btc:
            last_btc = btc["last"]
            if self._last_btc_price > 0:
                drop_pct = (last_btc - self._last_btc_price) / self._last_btc_price
                if drop_pct <= -0.03:
                    self.bus.publish("EMERGENCY_DUMP", tickers)
                    return
            self._last_btc_price = last_btc

        # 2. Мгновенная проверка исполнения лимитных ордеров (check_fills)
        fills = paper.check_fills(tickers)
        for f in fills:
            tp_pct = (f["tp"] - f["price"]) / f["price"] * 100
            sl_pct = (f["sl"] - f["price"]) / f["price"] * 100
            emode = f.get("entry_mode", "")
            ev_mode_str = "Ловец дна" if emode == "reversal" else ("Ракета" if emode == "rocket" else "Снайпер")
            paper.log_event(f["symbol"], "buy", f["price"], mode=ev_mode_str)
            
            self.bus.publish("NOTIFY", {
                "text": f"🛒 <b>Покупка</b> · {pair_html(f['symbol'], f)}\n💵 {usd(f['qty'] * f['price'])} · 📥 {fmt_price(f['price'])}\n🎯 TP {fmt_pct(tp_pct)} · 🛡 SL {fmt_pct(sl_pct)}",
                "urgent": False
            })

        # 3. Быстрый проход по открытым позициям (TP/SL/Трейлинг)
        state_changed = False
        
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t: continue
            last = t["last"]
            
            # SL
            if last <= pos["sl"]:
                ex = paper._sell(sym, last, "SL 🛡")
                paper.log_event(sym, "sell_loss", last, "SL 🛡")
                self.bus.publish("NOTIFY", {
                    "text": f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · SL 🛡\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}",
                    "urgent": False
                })
                continue
            
            # TP (Если TP1 не было - продаем половину, иначе кроем всё)
            if last >= pos["tp"]:
                if not pos.get("tp1_done"):
                    half = pos["qty"] / 2
                    ex = paper.sell_partial(sym, half, pos["tp"], "TP1 🎯")
                    pos["tp1_done"] = True
                    paper.log_event(sym, "sell_profit", last, "TP1 🎯")
                    shadow.mark_success(sym)
                    
                    breakeven_price = pos["avg"] * 1.002
                    pos["sl"] = max(pos["sl"], breakeven_price)
                    # Сдвигаем новый TP (динамический сдвиг на основе текущей цены)
                    pos["tp"] = round(pos["tp"] * 1.03, 10) 
                    state_changed = True
                    
                    self.bus.publish("NOTIFY", {
                        "text": f"🎯 <b>TP1</b> · {pair_html(sym, ex)} · 50%\n🔥 {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}\nостаток бежит · 🎯 {fmt_price(pos['tp'])} · 🔒 БУ",
                        "urgent": False
                    })
                else:
                    ex = paper._sell(sym, last, "TP ✅")
                    paper.log_event(sym, "sell_profit", last, "TP ✅")
                    shadow.mark_success(sym)
                    self.bus.publish("NOTIFY", {
                        "text": f"🎯 <b>TP RUNNER</b> · {pair_html(sym, ex)} · ✅\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}",
                        "urgent": False
                    })
                continue

            # Трейлинг-стоп (Сдвиг SL)
            max_p = max(pos.get("max_price", last), last)
            if max_p > pos.get("max_price", 0.0):
                pos["max_price"] = max_p
                state_changed = self._update_trailing_stop(sym, pos, max_p) or state_changed

        if state_changed:
            paper.save()

    def _update_trailing_stop(self, sym: str, pos: dict, max_p: float) -> bool:
        avg_p = pos["avg"]
        if avg_p <= 0:
            return False

        tier = pos.get("tier") or "SMALL"

        # Параметры трейлинга по весовым категориям
        TIER_TRAIL_CONFIG = {
            "TOP20": {"be_trigger": 0.007, "runner_dist": 0.008, "pre_tp_trigger": 1.5, "pre_tp_dist": 0.006},
            "MID":   {"be_trigger": 0.010, "runner_dist": 0.013, "pre_tp_trigger": 2.0, "pre_tp_dist": 0.010},
            "SMALL": {"be_trigger": 0.014, "runner_dist": 0.018, "pre_tp_trigger": 2.5, "pre_tp_dist": 0.014},
            "MICRO": {"be_trigger": 0.022, "runner_dist": 0.028, "pre_tp_trigger": 3.5, "pre_tp_dist": 0.020},
        }
        cfg = TIER_TRAIL_CONFIG.get(tier, TIER_TRAIL_CONFIG["SMALL"])

        current_sl = pos["sl"]
        new_sl = current_sl
        breakeven = avg_p * 1.0025  # Комиссия в обе стороны + минимальный буфер
        profit_pct = (max_p - avg_p) / avg_p * 100

        # 1. РАННИЙ БЕЗУБЫТОК: адаптирован под волатильность актива
        if max_p >= avg_p * (1 + cfg["be_trigger"]):
            if new_sl < breakeven:
                new_sl = breakeven

        # 2. ТРЕЙЛИНГ РАННЕРА (после взятия TP1):
        if pos.get("tp1_done"):
            runner_trail = max_p * (1 - cfg["runner_dist"])
            min_runner_sl = avg_p * 1.005  # Фиксация как минимум +0.5% чистыми
            new_sl = max(new_sl, runner_trail, min_runner_sl)
        else:
            # 3. ТРЕЙЛИНГ ДО TP1 (вертикальный безоткатный импульс):
            if profit_pct >= cfg["pre_tp_trigger"]:
                new_sl = max(new_sl, max_p * (1 - cfg["pre_tp_dist"]))

        # Проверка фактического смещения стопа
        if new_sl > current_sl:
            old_sl_str = fmt_price(current_sl)
            new_sl_str = fmt_price(new_sl)

            if new_sl_str != old_sl_str:
                pos["sl"] = round(new_sl, 8)
                pos["max_sl"] = pos["sl"]
                tag = "🔒 БУ" if (current_sl < breakeven and new_sl >= breakeven and not pos.get("tp1_done")) else f"Трейлинг SL ({tier})"
                paper.log_event(sym, "sl_moved", pos["sl"], tag)
                self.bus.publish("NOTIFY", {
                    "text": f"🛡 SL поднят по <b>{sym[:-4]}</b> до {new_sl_str} ({tag})", 
                    "urgent": False
                })
                return True

        return False
