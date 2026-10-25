import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import live_score, get_thresholds, _returns, is_post_pump_dump
from bot.strategy.indicators import atr, ema, find_sr_levels, get_all_sr_levels
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.strategy.risk import calc_initial_sl_tp, calc_runner_target, update_position_sl, get_trade_policy
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.sizing import entry_offset
from bot.utils.format import (
    pair_html, format_coin_compact, sl_emoji, grade_badge,
    usd, fmt_price, fmt_pct, pnl_emoji
)
from bot.core.state import bot_state


class PositionManagerWorker:
    """Управление позициями: Дышащий Коридор по свечам 15m/1H, защита лимиток от ножей и Freeze Zone."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.regime_queue = self.bus.subscribe("REGIME_UPDATED", maxsize=2)
        self.current_regime = "neutral"

    async def run(self):
        logger.info("🛡 Position Manager запущен: Дышащий Коридор и защита лимитных ордеров...")
        asyncio.create_task(self._regime_updater())

        while True:
            if bot_state.paused or not bot_state.trading_enabled:
                await asyncio.sleep(10)
                continue

            try:
                await self._maintenance_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"PositionManager error: {e}")

            await asyncio.sleep(60)

    async def _regime_updater(self):
        while True:
            event = await self.regime_queue.get()
            self.current_regime = event.payload["regime"]
            self.regime_queue.task_done()

    def _notify(self, text: str, urgent: bool = False):
        self.bus.publish("NOTIFY", {"text": text, "urgent": urgent})

    async def _maintenance_cycle(self):
        tickers = await market_data.get_tickers()
        deriv_tickers = await market_data.get_derivatives_tickers()
        if not tickers:
            return

        btc_candles = await market_data.get_kline("BTCUSDT", "15", 120)
        btc_ret = _returns([c["close"] for c in btc_candles])

        metrics_24h = paper.get_metrics(tickers, hours=24)
        old_mode = getattr(learner, "current_risk_mode", "NORMAL")
        learner.update_threshold(metrics_24h["profit_factor"], metrics_24h["max_drawdown_pct"], metrics_24h["total_trades"])
        new_mode = getattr(learner, "current_risk_mode", "NORMAL")

        if old_mode != new_mode and metrics_24h["total_trades"] > 0:
            mode_icons = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}
            self._notify(f"🎚 <b>Режим риска (ИИ) изменен:</b> {mode_icons.get(new_mode, '⚪')} {new_mode}\n<i>(строгость {learner.threshold_adj:+.2f})</i>")

        news_items = await fetch_news_cache()
        regime = self.current_regime
        thrs = get_thresholds(regime)
        policy = get_trade_policy(new_mode)
        current_time = int(time.time())

        # =====================================================================
        # 1. ПРОВЕРКА ОТКРЫТЫХ ПОЗИЦИЙ И ОБНОВЛЕНИЕ «ДЫШАЩЕГО КОРИДОРА»
        # =====================================================================
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t:
                continue

            score_pos, candles = await live_score(sym, t, regime, btc_ret, news_items, deriv_t=deriv_tickers.get(sym), is_open_pos=True, btc_candles=btc_candles)
            if score_pos is None or not candles:
                continue

            closes = [c["close"] for c in candles]
            a = atr(candles)
            if a <= 0:
                continue

            last = t["last"]
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            e21_series = ema(closes, 21)
            e21, e50 = e21_series[-1], ema(closes, 50)[-1]

            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            is_toxic = neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)

            if is_toxic:
                bot_state.set_cooldown(sym, 7200)
                if pnl_pct >= 1.0 or pnl_pct <= 0:
                    reason = "✂📰 Новости"
                    ex = paper._sell(sym, last, reason, regime_now=regime)
                    paper.log_event(sym, "sell", last, f"Новости {neg}/{mentions}", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {reason} {neg}/{mentions}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                    continue

            entry_mode = pos.get("entry_mode", "sniper")
            e21_falling = len(e21_series) >= 4 and e21 < e21_series[-3] * 0.9995
            closed_2_below = (
                len(candles) >= 3 and
                candles[-2]["close"] < (e50 - 0.55 * a) and
                candles[-3]["close"] < (e50 - 0.55 * a)
            )
            trend_broken = (
                closed_2_below and
                last < (e50 - 0.55 * a) and
                (e21 < e50 * 0.997 or e21_falling)
            )
            pos_age = current_time - pos.get("entry_time", current_time)

            should_exit_early = False
            if pos_age >= 1800 and not is_toxic:
                if entry_mode != "reversal" and trend_broken:
                    should_exit_early = True

            if should_exit_early:
                reason = "🪫 Слом тренда" if pnl_pct > 0 else "✂️📉 Слом тренда"
                ex = paper._sell(sym, last, reason, regime_now=regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, last, reason, tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                bot_state.record_trade_exit(sym, ex["pnl_pct"], last, is_sl=(ex["pnl_pct"] < -0.15))
                if ex["pnl"] > 0:
                    shadow.mark_success(sym)
                self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)}{grade_badge(ex)} · {reason}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                continue

            max_p = max(pos.get("max_price", last), last)
            pos["max_price"] = max_p
            sl_updated, new_sl, new_state, tag_emoji = update_position_sl(pos, max_p, last_price=last, candles_15m=candles)
            if sl_updated:
                old_sl_str = pos.get("_last_notified_sl_str") or fmt_price(pos.get("sl", 0.0))
                new_sl_str = fmt_price(new_sl)
                old_state = pos.get("_last_notified_sl_state") or pos.get("sl_state", "SHIELD")
                pos["sl"] = new_sl
                pos["sl_state"] = new_state
                pos["max_sl"] = new_sl
                paper.save()
                paper.log_event(sym, "sl_moved", pos["sl"], tag_emoji, tp=pos.get("tp"), sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))

                last_notify_ts = float(pos.get("_last_sl_notify_ts", 0.0))
                if new_sl_str != old_sl_str and ((current_time - last_notify_ts >= 180) or new_state != old_state):
                    pos["_last_notified_sl_str"] = new_sl_str
                    pos["_last_notified_sl_state"] = new_state
                    pos["_last_sl_notify_ts"] = current_time
                    coin_compact = format_coin_compact(sym, pos)
                    self._notify(f"🛡 SL поднят по {coin_compact} до {new_sl_str} ({tag_emoji})")

            if pos.get("tp1_done") and last >= (pos["tp"] - 0.5 * a):
                candles_1h = await market_data.get_kline(sym, "60", 250)
                next_target, can_expand, phase_reason = calc_runner_target(sym, last, candles, candles_1h, pos=pos)
                if can_expand and next_target > pos["tp"] * 1.006:
                    old_tp = pos["tp"]
                    pos["tp"] = next_target
                    pos["sl"] = max(pos["sl"], round(old_tp * 0.995, 8))
                    tp1_p = pos.get("tp1_price") or pos.get("initial_tp") or 0.0
                    pos["sl_state"] = "ABOVE_TP1" if (tp1_p > 0 and pos["sl"] >= tp1_p) else "TRAILING_RUNNER"
                    sl_tag = sl_emoji(pos["sl_state"])
                    paper.save()
                    paper.log_event(sym, "tp_runner_raised", pos["tp"], "TP🏃🏻‍➡️ поднят", mode="TP🏃🏻‍➡️", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self._notify(f"TP🏃🏻‍➡️ <b>Потолок коридора поднят</b> · {pair_html(sym, pos)}\n🎯 {fmt_price(pos['tp'])} · {sl_tag} {fmt_price(pos['sl'])}")

        # =====================================================================
        # 2. ПРОВЕРКА ОЖИДАЮЩИХ ОРДЕРОВ (Защита от падающих ножей + Freeze Zone)
        # =====================================================================
        for order in list(paper.orders):
            sym = order["symbol"]
            t = tickers.get(sym)
            if not t:
                continue

            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            if neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Токсичные новости", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 7200)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> токсичные новости ({neg}/{mentions})")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ (новости)")
                continue

            entry_mode = order.get("entry_mode", "sniper")
            patterns = order.get("patterns", [])

            score_now, candles = await live_score(
                sym, t, regime, btc_ret, news_items,
                deriv_t=deriv_tickers.get(sym),
                saved_patterns=patterns,
                saved_mode=entry_mode,
                btc_candles=btc_candles,
            )
            if score_now is None or not candles:
                continue

            closes = [c["close"] for c in candles]
            e21_series = ema(closes, 21)
            e21, e50 = e21_series[-1], ema(closes, 50)[-1]
            a = atr(candles)
            if a <= 0:
                continue

            order_age = current_time - order["created"]
            tier = order.get("tier") or "SMALL"
            is_accum = "vcp" in patterns
            thr = thrs.get(entry_mode, 6.0)
            init_score = order.get("initial_score", order.get("score", thr))

            is_post_pump, pp_reason = is_post_pump_dump(candles)
            if is_post_pump:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Пост-памп слив ({pp_reason})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 1800)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ☠️⚰️ {pp_reason}")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ (пост-памп слив)")
                continue

            curr_c = candles[-1]
            closed_c = candles[-2]
            heavy_dump_candle = (
                (curr_c["open"] - curr_c["close"]) > 0.65 * a or
                (closed_c["open"] - closed_c["close"] > 0.65 * a and curr_c["close"] < closed_c["close"])
            )
            ord_sup = order.get("nearest_sup")
            sup_broken = bool(ord_sup and closed_c["close"] < ord_sup * 0.995 and t["last"] < ord_sup * 0.995)

            base_intact = (entry_mode != "reversal" and t["last"] >= e50 * 0.996 and e21 >= e50 * 0.996)
            trend_broken_order = (
                (entry_mode != "reversal" and ((t["last"] < e50 * 0.992 and e21 < e50 * 0.998) or (heavy_dump_candle and t["last"] < e21 * 0.997))) or
                (entry_mode == "reversal" and (sup_broken or (curr_c["open"] - curr_c["close"]) > 0.85 * a))
            )

            if trend_broken_order:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Слом структуры ({init_score:.1f}-{score_now:.1f})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 900)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ☠️⚰️ слом структуры/нож ({init_score:.1f} -> {score_now:.1f})")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ ({init_score:.1f}-{score_now:.1f})")
                continue

            if order_age >= 900 and not base_intact and score_now < (thr - 1.5):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], f"Ослаб ({score_now:.1f})", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 600)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> 🪫📉 ослаб сигнал")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🪫📉 ({score_now:.1f})")
                continue

            if t["last"] > order["price"] + 2.2 * a:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Улетела без нас", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 300)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> 🚀👋🏻 улетела без нас")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🚀👋🏻")
                continue

            if order_age > 7200:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Тайм-аут 2ч", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 600)
                logger.info(f"PositionManager: ⚠️ Ордер {sym} снят -> ☠️⚰️ тайм-аут 2ч")
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ☠️⚰️ ({init_score:.1f}-{score_now:.1f})")
                continue

            last_move_ts = order.get("last_move_ts", order["created"])
            if (current_time - last_move_ts) < 480:
                continue

            atr_pct = a / t["last"] * 100
            off = entry_offset(score_now, thr, regime, atr_pct, entry_mode, tier=tier, is_accumulation=is_accum)
            ideal_price = t["last"] * (1 + off)
            old_price = order["price"]

            candles_1h = await market_data.get_kline(sym, "60", 250)
            nearest_res, res_touches, nearest_sup, sup_touches = find_sr_levels(candles, candles_1h)
            live_sup = nearest_sup if (nearest_sup and sup_touches >= 2) else order.get("nearest_sup")
            live_sup_touches = sup_touches if (nearest_sup and sup_touches >= 2) else order.get("sup_touches", 2 if order.get("nearest_sup") else 0)

            target_price = old_price
            action_type = None
            price_icon = ""
            dev_pct = abs(ideal_price - old_price) / old_price * 100
            in_freeze_zone = abs(t["last"] - old_price) <= (0.45 * a) or (abs(t["last"] - old_price) / old_price * 100) <= 0.38

            if ideal_price < old_price and dev_pct >= 0.40 and not in_freeze_zone and not heavy_dump_candle:
                if not (live_sup and ideal_price < live_sup * 1.0015):
                    target_price = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"

            elif ideal_price > old_price and dev_pct >= 0.38 and not in_freeze_zone and regime != "bear" and new_mode != "STRICT":
                can_shift_up = (is_accum and t["last"] <= (e50 * 1.012)) or (entry_mode == "sniper" and t["last"] <= (e21 * 1.008))
                if can_shift_up:
                    target_price = ideal_price
                    action_type = "hunt_up"
                    price_icon = "⬆️"

            if not action_type:
                continue

            all_res, all_sup = get_all_sr_levels(candles, candles_1h)
            res_above_target = [(r[0], r[1]) for r in all_res if r[0] > target_price * 1.0002]
            if res_above_target:
                res_above_target.sort(key=lambda x: x[0])
                entry_res, entry_res_touches = res_above_target[0]
            else:
                entry_res, entry_res_touches = None, 0

            sup_below_target = [(s[0], s[1]) for s in all_sup if s[0] <= target_price * 1.0010]
            if sup_below_target:
                sup_below_target.sort(key=lambda x: x[0], reverse=True)
                entry_sup, entry_sup_touches = sup_below_target[0]
            else:
                entry_sup, entry_sup_touches = live_sup, live_sup_touches

            min_rr = policy["min_rr_sniper"] if entry_mode == "sniper" else policy["min_rr_alt"]
            recent_low_6 = min(c["low"] for c in candles[-6:]) if len(candles) >= 6 else None

            closes_1h = [c["close"] for c in candles_1h] if candles_1h and len(candles_1h) >= 50 else []
            e21_1h_val = ema(closes_1h, 21)[-1] if closes_1h else order.get("e21_1h")
            e50_1h_val = ema(closes_1h, 50)[-1] if closes_1h else order.get("e50_1h")

            sl, tp, is_valid, _ = calc_initial_sl_tp(
                symbol=sym, entry=target_price, tier=tier, a=a,
                sup=entry_sup, sup_touches=entry_sup_touches,
                res=entry_res, res_touches=entry_res_touches,
                is_mom=(entry_mode == "rocket"), entry_mode=entry_mode,
                min_rr=min_rr,
                recent_low_6=recent_low_6,
                ema50=e50,
                e21_1h=e21_1h_val,
                e50_1h=e50_1h_val,
            )

            if is_valid and action_type:
                old_notional = order["qty"] * order["price"]
                order["price"] = target_price
                order["qty"] = old_notional / target_price if target_price > 0 else order["qty"]
                order["sl"] = sl
                order["tp"] = tp
                order["nearest_sup"] = entry_sup
                order["sup_touches"] = entry_sup_touches
                order["nearest_res"] = entry_res
                order["res_touches"] = entry_res_touches
                order["last_move_ts"] = current_time
                order["requotes"] = order.get("requotes", 0) + 1
                paper.save()

                msg_desc = "база" if action_type == "hunt_up" else f"{dev_pct:.2f}%"
                logger.info(f"PositionManager: 📐 Сдвиг ордера {sym} ({action_type}) -> new_entry=${fmt_price(target_price)}, SL=${fmt_price(sl)}")
                paper.log_event(sym, "order_moved", order["price"], f"Сдвиг: {action_type}", tp=order["tp"], sl=order["sl"], sup=entry_sup, res=entry_res)
                self._notify(f"📐 <b>Сдвиг {price_icon}</b> · {pair_html(sym, order)}\n📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · 🛡📉 {fmt_price(order['sl'])} · {msg_desc}")
