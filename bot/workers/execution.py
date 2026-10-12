import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.core.state import bot_state
from bot.exchange.paper_exchange import paper
from bot.exchange.market_data import market_data
from bot.utils.format import pair_html, usd, fmt_price, fmt_pct, pnl_emoji
from bot.strategy.shadow import shadow
from bot.strategy.risk import update_position_sl, calc_runner_target

class ExecutionRiskWorker:
    """Критический воркер исполнения ордеров, машины состояний SL и фиксации TP."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED", maxsize=5)
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP", maxsize=1)
        self._last_btc_price = 0.0

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: машина состояний SL и исполнение позиций...")
        await asyncio.gather(
            self._price_loop(),
            self._emergency_loop()
        )

    async def _price_loop(self) -> None:
        while True:
            try:
                event = await self.price_queue.get()
                tickers = event.payload
                await self._process_tick(tickers)
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
                    bot_state.set_cooldown(ex['symbol'], 900)
                    self.bus.publish("NOTIFY", {"text": f"🚨 <b>Экстренный выход</b> · {ex['symbol']} · {ex['pnl_pct']:.2f}%", "urgent": True})

                self.emergency_queue.task_done()
            except Exception as e:
                logger.error(f"Execution _emergency_loop error: {e}")

    async def _process_tick(self, tickers: dict) -> None:
        btc = tickers.get("BTCUSDT")
        if btc:
            last_btc = btc["last"]
            if self._last_btc_price > 0:
                drop_pct = (last_btc - self._last_btc_price) / self._last_btc_price
                if drop_pct <= -0.03:
                    self.bus.publish("EMERGENCY_DUMP", tickers)
                    return
            self._last_btc_price = last_btc

        fills = paper.check_fills(tickers)
        for f in fills:
            tp_pct = (f["tp"] - f["price"]) / f["price"] * 100
            sl_pct = (f["sl"] - f["price"]) / f["price"] * 100
            emode = f.get("entry_mode", "")
            ev_mode_str = "Ловец дна" if emode == "reversal" else ("Ракета" if emode == "rocket" else "Снайпер")
            paper.log_event(f["symbol"], "buy", f["price"], mode=ev_mode_str, tp=f.get("tp"), sl=f.get("sl"), sup=f.get("nearest_sup"), res=f.get("nearest_res"))

            self.bus.publish("NOTIFY", {
                "text": f"🛒 <b>Покупка</b> · {pair_html(f['symbol'], f)}\n💵 {usd(f['qty'] * f['price'])} · 📥 {fmt_price(f['price'])}\n🎯 TP {fmt_pct(tp_pct)} · 🛡 SL {fmt_pct(sl_pct)}",
                "urgent": False
            })

        state_changed = False

        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t: continue
            last = t["last"]

            # SL
            if last <= pos["sl"]:
                ex = paper._sell(sym, last, "SL 🛡", regime_now=bot_state.current_regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, last, "SL 🛡", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                bot_state.set_cooldown(sym, 420)
                self.bus.publish("NOTIFY", {
                    "text": f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · SL 🛡\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}",
                    "urgent": False
                })
                continue

            # TP
            if last >= pos["tp"]:
                if not pos.get("tp1_done"):
                    half = pos["qty"] / 2
                    ex = paper.sell_partial(sym, half, pos["tp"], "TP1 🎯")
                    pos["tp1_done"] = True
                    paper.log_event(sym, "tp1", last, f"TP1 (+{ex['pnl_pct']:.1f}%)", mode="TP1 🎯", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    shadow.mark_success(sym)

                    # Перевод в безубыток и расчет цели для Runner через уровни старшего ТФ
                    candles_15m = await market_data.get_kline(sym, "15", 120)
                    candles_1h = await market_data.get_kline(sym, "60", 250)
                    pos["tp"] = calc_runner_target(sym, last, candles_15m, candles_1h)

                    pos["sl_state"] = "BREAKEVEN"
                    pos["sl"] = max(pos["sl"], round(pos["avg"] * 1.0025, 8))
                    pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                    pos["_last_notified_sl_state"] = "BREAKEVEN"
                    state_changed = True

                    self.bus.publish("NOTIFY", {
                        "text": f"🎯 <b>TP1</b> · {pair_html(sym, ex)} · 50%\n🔥 {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}\nостаток бежит · 🎯 {fmt_price(pos['tp'])} · 🔒 БУ",
                        "urgent": False
                    })
                else:
                    candles_15m = await market_data.get_kline(sym, "15", 120)
                    candles_1h = await market_data.get_kline(sym, "60", 250)
                    next_target = calc_runner_target(sym, last, candles_15m, candles_1h)
                    
                    if next_target > pos["tp"] * 1.008:
                        old_tp = pos["tp"]
                        pos["tp"] = next_target
                        pos["sl"] = max(pos["sl"], round(old_tp * 0.997, 8))
                        pos["sl_state"] = "TRAILING_RUNNER"
                        pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                        pos["_last_notified_sl_state"] = "TRAILING_RUNNER"
                        state_changed = True
                        paper.log_event(sym, "tp_runner_raised", pos["tp"], "TP Runner поднят к следующей стене", mode="TP поднят 🎯", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        self.bus.publish("NOTIFY", {
                            "text": f"🎯 <b>TP Runner поднят к следующему уровню</b> · {pair_html(sym, pos)}\n🎯 Новая цель: {fmt_price(pos['tp'])} · 🛡 Стоп: {fmt_price(pos['sl'])}",
                            "urgent": False
                        })
                    else:
                        ex = paper._sell(sym, last, "TP ✅", regime_now=bot_state.current_regime)
                        paper.log_event(sym, "tp_runner", last, f"TP RUNNER (+{ex['pnl_pct']:.1f}%)", mode="TP RUNNER 🚀", tp=last, sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        shadow.mark_success(sym)
                        bot_state.set_cooldown(sym, 180)
                        self.bus.publish("NOTIFY", {
                            "text": f"🎯 <b>TP RUNNER</b> · {pair_html(sym, ex)} · ✅\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}",
                            "urgent": False
                        })
                continue

            # ⚡ ТРЕЙЛИНГ И МАШИНА СОСТОЯНИЙ СТОПА (С АНТИ-СПАМ ЗАЩИТОЙ)
            max_p = max(pos.get("max_price", last), last)
            pos["max_price"] = max_p

            sl_updated, new_sl, new_state, tag_desc = update_position_sl(pos, max_p)
            if sl_updated:
                old_sl_str = pos.get("_last_notified_sl_str") or fmt_price(pos.get("sl", 0.0))
                new_sl_str = fmt_price(new_sl)
                old_state = pos.get("_last_notified_sl_state") or pos.get("sl_state", "SHIELD")

                pos["sl"] = new_sl
                pos["sl_state"] = new_state
                pos["max_sl"] = new_sl
                state_changed = True

                # ⚡ АНТИ-СПАМ: Уведомляем ТОЛЬКО если отображаемая цена или статус реально изменились!
                if new_sl_str != old_sl_str or new_state != old_state:
                    pos["_last_notified_sl_str"] = new_sl_str
                    pos["_last_notified_sl_state"] = new_state
                    paper.log_event(sym, "sl_moved", pos["sl"], tag_desc, tp=pos.get("tp"), sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self.bus.publish("NOTIFY", {
                        "text": f"🛡 SL поднят по <b>{sym[:-4]}</b> до {new_sl_str} ({tag_desc})", 
                        "urgent": False
                    })

        if state_changed:
            paper.save()
