import time
import asyncio
from loguru import logger
from bot.core.event_bus import EventBus
from bot.core.state import bot_state
from bot.exchange.paper_exchange import paper
from bot.exchange.market_data import market_data
from bot.utils.format import (
    pair_html, format_coin_compact, sl_emoji, grade_badge,
    usd, fmt_price, fmt_pct, pnl_emoji
)
from bot.strategy.shadow import shadow
from bot.strategy.risk import update_position_sl, calc_runner_target


class ExecutionRiskWorker:
    """Критический воркер исполнения ордеров, «Дышащего Коридора» SL/TP и антиспам-нотификаций."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED", maxsize=5)
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP", maxsize=1)
        self._last_btc_price = 0.0

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: Дышащий Коридор SL/TP и исполнение позиций...")
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
                    bot_state.set_cooldown(ex["symbol"], 900)
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
                "text": (
                    f"🛒 <b>Покупка</b> · {pair_html(f['symbol'], f)}{grade_badge(f)}\n"
                    f"💵 {usd(f['qty'] * f['price'])} · 📥 {fmt_price(f['price'])}\n"
                    f"🎯 TP {fmt_pct(tp_pct)} · 🛡📉 {fmt_pct(sl_pct)}"
                ),
                "urgent": False,
            })

        state_changed = False
        now_ts = time.time()

        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t:
                continue
            last = t["last"]

            # 1. Исполнение стоп-лосса с точным указанием типа стопа (🛡📉 / 🛡🔒 / 🛡💼 / 🛡📈 / 🛡🔥)
            if last <= pos["sl"]:
                sl_exec_price = pos["sl"]
                sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
                ex = paper._sell(sym, sl_exec_price, f"SL {sl_tag}", regime_now=bot_state.current_regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, sl_exec_price, f"SL {sl_tag}", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                is_real_sl_loss = ex["pnl_pct"] < -0.15
                bot_state.record_trade_exit(sym, ex["pnl_pct"], sl_exec_price, is_sl=is_real_sl_loss)

                self.bus.publish("NOTIFY", {
                    "text": (
                        f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {ex['exit_type']}\n"
                        f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                    ),
                    "urgent": False,
                })
                continue

            # 2. Исполнение тейк-профита (🔥 TP1 и конвейер 🛡🔥 / TP🏃🏻‍➡️ без обрезки ракет на взлёте)
            if last >= pos["tp"]:
                candles_15m = await market_data.get_kline(sym, "15", 120)
                candles_1h = await market_data.get_kline(sym, "60", 250)

                if not pos.get("tp1_done"):
                    # Фиксируем 40% (для тренда) и 50% (для реверсала), оставляя 60%/50% на 🛡🔥 и TP🏃🏻‍➡️
                    tp1_ratio = 0.50 if pos.get("entry_mode") == "reversal" else 0.40
                    part_qty = pos["qty"] * tp1_ratio
                    ex = paper.sell_partial(sym, part_qty, pos["tp"], "🔥 TP1")
                    pos["tp1_done"] = True
                    paper.log_event(sym, "tp1", pos["tp"], f"🔥 TP1 (+{ex['pnl_pct']:.1f}%)", mode="🔥 TP1", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    shadow.mark_success(sym)

                    next_target, _, _ = calc_runner_target(sym, last, candles_15m, candles_1h, pos=pos)
                    pos["tp"] = max(pos["tp"] * 1.015, next_target)

                    _, new_sl, new_state, _ = update_position_sl(pos, max(pos.get("max_price", last), last), last_price=last, candles_15m=candles_15m)
                    pos["sl"] = new_sl
                    pos["sl_state"] = new_state
                    pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                    pos["_last_notified_sl_state"] = new_state
                    pos["_last_sl_notify_ts"] = now_ts
                    state_changed = True

                    pct_label = int(round(tp1_ratio * 100))
                    self.bus.publish("NOTIFY", {
                        "text": (
                            f"🔥 <b>TP1</b> · {pair_html(sym, ex)}{grade_badge(pos)} · {pct_label}%\n"
                            f"🟢 {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}\n"
                            f"TP🏃🏻‍➡️ {fmt_price(pos['tp'])} · {sl_emoji(new_state)} {fmt_price(pos['sl'])}"
                        ),
                        "urgent": False,
                    })
                else:
                    next_target, can_expand, phase_reason = calc_runner_target(sym, last, candles_15m, candles_1h, pos=pos)

                    if can_expand and next_target > pos["tp"] * 1.008:
                        old_tp = pos["tp"]
                        pos["tp"] = next_target
                        pos["sl"] = max(pos["sl"], round(old_tp * 0.995, 8))
                        tp1_p = pos.get("tp1_price") or pos.get("initial_tp") or 0.0
                        new_state = "ABOVE_TP1" if (tp1_p > 0 and pos["sl"] >= tp1_p) else "TRAILING_RUNNER"
                        pos["sl_state"] = new_state
                        pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                        pos["_last_notified_sl_state"] = new_state
                        pos["_last_sl_notify_ts"] = now_ts
                        state_changed = True
                        sl_tag = sl_emoji(new_state)
                        paper.log_event(sym, "tp_runner_raised", pos["tp"], "TP🏃🏻‍➡️ поднят по коридору", mode="TP🏃🏻‍➡️", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        self.bus.publish("NOTIFY", {
                            "text": (
                                f"TP🏃🏻‍➡️ <b>Потолок коридора расширен</b> · {pair_html(sym, pos)}\n"
                                f"🎯 {fmt_price(pos['tp'])} · {sl_tag} {fmt_price(pos['sl'])}"
                            ),
                            "urgent": False,
                        })
                    elif phase_reason in ("climax_freeze", "shelf_freeze"):
                        # Вместо принудительной рыночной продажи на импульсе (кейс AXL) зажимаем ультра-стоп 🛡🔥 и двигаем потолок выше!
                        old_tp = pos["tp"]
                        candle_low = candles_15m[-1]["low"] if candles_15m else last * 0.993
                        tight_sl = round(max(pos["sl"], old_tp * 0.995, candle_low * 0.998, last * 0.993), 8)
                        if tight_sl >= last * 0.997:
                            tight_sl = round(last * 0.995, 8)
                        pos["sl"] = tight_sl
                        pos["sl_state"] = "ABOVE_TP1"
                        pos["tp"] = round(max(old_tp, last) * 1.018, 8)
                        pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                        pos["_last_notified_sl_state"] = "ABOVE_TP1"
                        pos["_last_sl_notify_ts"] = now_ts
                        state_changed = True
                        paper.log_event(sym, "tp_runner_raised", pos["tp"], f"🛡🔥 Ультра-трейлинг ({phase_reason})", mode="🛡🔥", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        self.bus.publish("NOTIFY", {
                            "text": (
                                f"🛡🔥 <b>Ультра-трейлинг на импульсе</b> · {pair_html(sym, pos)}\n"
                                f"TP🏃🏻‍➡️ {fmt_price(pos['tp'])} · 🛡🔥 {fmt_price(pos['sl'])}"
                            ),
                            "urgent": False,
                        })
                    else:
                        tp_exec_price = pos["tp"]
                        ex = paper._sell(sym, tp_exec_price, "TP🏃🏻‍➡️", regime_now=bot_state.current_regime)
                        paper.log_event(sym, "tp_runner", tp_exec_price, f"TP🏃🏻‍➡️ (+{ex['pnl_pct']:.1f}%, {phase_reason})", mode="TP🏃🏻‍➡️", tp=tp_exec_price, sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        shadow.mark_success(sym)
                        bot_state.record_trade_exit(sym, ex["pnl_pct"], tp_exec_price, is_sl=False)
                        self.bus.publish("NOTIFY", {
                            "text": (
                                f"TP🏃🏻‍➡️ <b>Фиксация на пике</b> · {pair_html(sym, ex)}{grade_badge(ex)}\n"
                                f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                            ),
                            "urgent": False,
                        })
                continue

            # 3. Асимметричный храповик SL по Дышащему Коридору + Антиспам 3 минуты
            max_p = max(pos.get("max_price", last), last)
            pos["max_price"] = max_p

            sl_updated, new_sl, new_state, tag_emoji = update_position_sl(pos, max_p, last_price=last)
            if sl_updated:
                old_sl = float(pos.get("sl", 0.0))
                old_sl_str = pos.get("_last_notified_sl_str") or fmt_price(old_sl)
                new_sl_str = fmt_price(new_sl)
                old_state = pos.get("_last_notified_sl_state") or pos.get("sl_state", "SHIELD")

                pos["sl"] = new_sl
                pos["sl_state"] = new_state
                pos["max_sl"] = new_sl
                state_changed = True

                paper.log_event(sym, "sl_moved", pos["sl"], tag_emoji, tp=pos.get("tp"), sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                last_notify_ts = float(pos.get("_last_sl_notify_ts", 0.0))
                a15 = float(pos.get("_atr15", 0.0))
                big_jump = (a15 > 0 and (new_sl - old_sl) >= 1.5 * a15)
                state_switched = (new_state != old_state)

                if new_sl_str != old_sl_str and ((now_ts - last_notify_ts >= 180.0) or state_switched or big_jump):
                    pos["_last_notified_sl_str"] = new_sl_str
                    pos["_last_notified_sl_state"] = new_state
                    pos["_last_sl_notify_ts"] = now_ts
                    coin_compact = format_coin_compact(sym, pos)
                    self.bus.publish("NOTIFY", {
                        "text": f"🛡 SL поднят по {coin_compact} до {new_sl_str} ({tag_emoji})",
                        "urgent": False,
                    })

        if state_changed:
            paper.save()
