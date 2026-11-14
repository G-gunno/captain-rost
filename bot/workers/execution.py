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
from bot.strategy.risk import update_position_sl, calc_runner_target, _round_px


class ExecutionRiskWorker:
    """Критический воркер исполнения ордеров, Многоступенчатого Коридора SL/TP и антиспам-нотификаций."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.price_queue = self.bus.subscribe("PRICE_UPDATED", maxsize=5)
        self.emergency_queue = self.bus.subscribe("EMERGENCY_DUMP", maxsize=1)
        self._btc_history = []

    async def run(self) -> None:
        logger.info("⚡ Execution Worker запущен: Многоступенчатый Коридор SL/TP и исполнение позиций...")
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
            except asyncio.CancelledError:
                break
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
                    bot_state.set_cooldown(ex["symbol"], 1800)
                    self.bus.publish("NOTIFY", {"text": f"🚨 <b>Экстренный выход</b> · {ex['symbol']} · {ex['pnl_pct']:.2f}%", "urgent": True})

                self.emergency_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Execution _emergency_loop error: {e}")

    async def _process_tick(self, tickers: dict) -> None:
        btc = tickers.get("BTCUSDT")
        now_ts = time.time()
        if btc:
            last_btc = btc["last"]
            self._btc_history.append((now_ts, last_btc))
            self._btc_history = [x for x in self._btc_history if now_ts - x[0] <= 300]
            if len(self._btc_history) >= 2:
                oldest_p = self._btc_history[0][1]
                drop_pct = (last_btc - oldest_p) / oldest_p
                if drop_pct <= -0.03:
                    self.bus.publish("EMERGENCY_DUMP", tickers)
                    return

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

        for ac in paper.pop_auto_cancelled():
            self.bus.publish("NOTIFY", {
                "text": f"ℹ️ <b>Снят резервный ордер</b> · {pair_html(ac['symbol'], ac)}{grade_badge(ac)} · тир {ac.get('tier', 'SMALL')} заполнен (3/3)",
                "urgent": False,
            })

        state_changed = False

        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t:
                continue
            last = t["last"]
            pos["symbol"] = sym

            if last <= pos["sl"]:
                sl_exec_price = pos["sl"]
                sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
                ex = paper._sell(sym, sl_exec_price, f"SL {sl_tag}", regime_now=bot_state.current_regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, sl_exec_price, f"SL {sl_tag}", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                is_real_sl_loss = ex["pnl_pct"] < -0.25
                bot_state.record_trade_exit(sym, ex["pnl_pct"], sl_exec_price, is_sl=is_real_sl_loss)

                self.bus.publish("NOTIFY", {
                    "text": (
                        f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {ex['exit_type']}\n"
                        f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                    ),
                    "urgent": False,
                })
                continue

            if last >= pos["tp"]:
                candles_15m = await market_data.get_kline(sym, "15", 120)
                candles_1h = await market_data.get_kline(sym, "60", 250)

                if not pos.get("tp1_done"):
                    dna = shadow.get_coin_dna(sym)
                    pos["wave_class"] = dna["wave_class"]
                    if pos.get("entry_mode") == "reversal":
                        tp1_ratio = 0.50 if dna["wave_class"] == "short_wave" else 0.42
                    else:
                        tp1_ratio = dna["tp1_ratio"]

                    part_qty = pos["qty"] * tp1_ratio
                    ex = paper.sell_partial(sym, part_qty, pos["tp"], "🔥 TP1")
                    pos["tp1_done"] = True
                    pos["tp1_time"] = int(now_ts)
                    pos["runner_steps"] = 0
                    paper.log_event(sym, "tp1", pos["tp"], f"🔥 TP1 (+{ex['pnl_pct']:.1f}%)", mode="🔥 TP1", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    shadow.mark_success(sym)

                    next_target, _, _ = calc_runner_target(sym, last, candles_15m, candles_1h, pos=pos)
                    pos["tp"] = _round_px(max(last * 1.0080, next_target))

                    _, new_sl, new_state, _ = update_position_sl(pos, max(pos.get("max_price", last), last), last_price=last, candles_15m=candles_15m)
                    pos["sl"] = new_sl
                    pos["sl_state"] = new_state
                    pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                    pos["_last_notified_sl_state"] = new_state
                    pos["_last_sl_notify_ts"] = now_ts
                    state_changed = True
                    paper.log_event(sym, "tp_runner_raised", pos["tp"], "TP🏃🏻‍➡️ ступень 1", mode="TP🏃🏻‍➡️", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                    pct_label = int(round(tp1_ratio * 100))
                    runner_label = 100 - pct_label
                    w_tag = "🌊 дальняя волна" if dna["wave_class"] == "long_wave" else ("⚡ короткая волна" if dna["wave_class"] == "short_wave" else "〰️ средняя волна")
                    self.bus.publish("NOTIFY", {
                        "text": (
                            f"🔥 <b>TP1 ({pct_label}%)</b> · {pair_html(sym, ex)}{grade_badge(pos)} · в раннер {runner_label}% ({w_tag})\n"
                            f"🟢 {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}\n"
                            f"TP🏃🏻‍➡️ {fmt_price(pos['tp'])} · {sl_emoji(new_state)} {fmt_price(pos['sl'])}"
                        ),
                        "urgent": False,
                    })
                else:
                    # МНОГОСТУПЕНЧАТЫЙ КОРИДОР НА ПАМПЕ (фикс CAPUSDT +18%):
                    # При пробое планки TP🏃🏻‍➡️ НЕ закрываем всю сделку в начале ракеты!
                    # Фиксируем 25% остатка, сдвигаем ступеньку TP🏃🏻‍➡️ выше (+0.85%..+1.15%) и поджимаем SL прямо под пробой (tp_hit * 0.9945)!
                    curr_c = candles_15m[-1] if candles_15m else None
                    c_rng = (curr_c["high"] - curr_c["low"]) if curr_c else 0.0
                    is_green_push = bool(curr_c and curr_c["close"] >= curr_c["open"] and (c_rng <= 0 or (curr_c["high"] - curr_c["close"]) <= 0.40 * c_rng))
                    r_steps = int(pos.get("runner_steps", 0))
                    remaining_notional = pos["qty"] * last

                    if is_green_push and r_steps < 5 and remaining_notional >= 18.0:
                        tp_hit_price = pos["tp"]
                        step_num = r_steps + 2
                        part_qty = pos["qty"] * 0.25
                        ex = paper.sell_partial(sym, part_qty, tp_hit_price, f"🔥 TP{step_num} Ступень")
                        pos["runner_steps"] = r_steps + 1

                        a15 = float(pos.get("_atr15") or (last * 0.008))
                        # Поджимаем SL прямо под пробитую планку (-0.50%), чтобы если это одиночный прокол — забрать профит на откате,
                        # а если это памп (CAP, ARX, INJ) — ехать дальше по ступенькам!
                        pos["sl"] = _round_px(max(pos["sl"], tp_hit_price * 0.9948, last - 0.50 * a15))
                        pos["sl_state"] = "ABOVE_TP1"
                        pos["tp"] = _round_px(max(last * 1.0085, last + 0.95 * a15))
                        pos["_last_notified_sl_str"] = fmt_price(pos["sl"])
                        pos["_last_notified_sl_state"] = "ABOVE_TP1"
                        pos["_last_sl_notify_ts"] = now_ts
                        state_changed = True

                        paper.log_event(sym, "tp_runner_raised", pos["tp"], f"🔥 TP{step_num} Ступень (+{ex['pnl_pct']:.1f}%)", mode="TP🏃🏻‍➡️", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        self.bus.publish("NOTIFY", {
                            "text": (
                                f"🔥 <b>TP{step_num} Ступень (25% остатка)</b> · {pair_html(sym, pos)}{grade_badge(pos)}\n"
                                f"🟢 {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}\n"
                                f"TP🏃🏻‍➡️ {fmt_price(pos['tp'])} · 🛡🔥 {fmt_price(pos['sl'])}"
                            ),
                            "urgent": False,
                        })
                    else:
                        tp_exec_price = pos["tp"]
                        ex = paper._sell(sym, tp_exec_price, "TP🏃🏻‍➡️", regime_now=bot_state.current_regime)
                        paper.log_event(sym, "tp_runner", tp_exec_price, f"TP🏃🏻‍➡️ Фиксация на проколе (+{ex['pnl_pct']:.1f}%)", mode="TP🏃🏻‍➡️", tp=tp_exec_price, sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                        shadow.mark_success(sym)
                        bot_state.record_trade_exit(sym, ex["pnl_pct"], tp_exec_price, is_sl=False)
                        self.bus.publish("NOTIFY", {
                            "text": (
                                f"TP🏃🏻‍➡️ <b>Фиксация на проколе</b> · {pair_html(sym, ex)}{grade_badge(ex)}\n"
                                f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                            ),
                            "urgent": False,
                        })
                continue

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
