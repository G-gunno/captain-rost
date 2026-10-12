import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import live_score, get_thresholds, _returns
from bot.strategy.indicators import atr, ema, find_sr_levels
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.strategy.risk import calc_initial_sl_tp, calc_runner_target
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.sizing import entry_offset
from bot.utils.format import pair_html, usd, fmt_price, fmt_pct, pnl_emoji
from bot.core.state import bot_state

class PositionManagerWorker:
    """Управление позициями: охрана без ложных SL_REGIME, лестничный Runner и безопасная охота."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.regime_queue = self.bus.subscribe("REGIME_UPDATED", maxsize=2)
        self.current_regime = "neutral"

    async def run(self):
        logger.info("🛡 Position Manager запущен: охрана позиций без SL_REGIME и безопасные реквоты...")
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
        if not tickers: return

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
        current_time = int(time.time())

        # 1. Проверка открытых позиций
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t: continue

            score_pos, candles = await live_score(sym, t, regime, btc_ret, news_items, deriv_t=deriv_tickers.get(sym), is_open_pos=True)
            if score_pos is None: continue

            closes = [c["close"] for c in candles]
            a = atr(candles)
            if a <= 0: continue

            last = t["last"]
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]

            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            is_toxic = neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)

            if is_toxic:
                bot_state.set_cooldown(sym, 7200)
                if pnl_pct >= 1.0 or pnl_pct <= 0:
                    reason = "✂📰 Новости"
                    ex = paper._sell(sym, last, reason, regime_now=regime)
                    paper.log_event(sym, "sell", last, f"Новости {neg}/{mentions}", tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason} {neg}/{mentions}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                    continue

            entry_mode = pos.get("entry_mode", "sniper")
            trend_broken = (last < e50 and e21 < (e50 * 0.998))
            pos_age = current_time - pos.get("entry_time", current_time)

            # ⚡ ТОЛЬКО подтвержденный слом тренда по конкретной монете (без выхода по SL_REGIME!)
            should_exit_early = False
            if pos_age >= 900 and not is_toxic:
                if entry_mode != "reversal" and trend_broken:
                    should_exit_early = True

            if should_exit_early:
                reason = "🪫 Слом тренда" if pnl_pct > 0 else "✂️📉 Слом тренда"
                bot_state.set_cooldown(sym, 420)
                ex = paper._sell(sym, last, reason, regime_now=regime)
                ev_type = "sell_profit" if ex["pnl"] > 0 else "sell_loss"
                paper.log_event(sym, ev_type, last, reason, tp=pos.get("tp"), sl=pos.get("sl"), sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                if ex["pnl"] > 0: shadow.mark_success(sym)
                self._notify(f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason}\n{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}")
                continue

            # ⚡ ЛЕСТНИЧНЫЙ БЕСКОНЕЧНЫЙ TP_RUNNER: подъем цели под следующее сопротивление
            if pos.get("tp1_done") and last >= (pos["tp"] - 0.4 * a):
                candles_1h = await market_data.get_kline(sym, "60", 250)
                next_target = calc_runner_target(sym, last, candles, candles_1h)
                if next_target > pos["tp"]:
                    old_tp = pos["tp"]
                    pos["tp"] = next_target
                    # Подтягиваем стоп-лосс под старый пробитый тейк
                    pos["sl"] = max(pos["sl"], old_tp * 0.997)
                    pos["sl_state"] = "TRAILING_RUNNER"
                    paper.save()
                    paper.log_event(sym, "tp_runner_raised", pos["tp"], "TP Runner поднят", mode="TP поднят 🎯", tp=pos["tp"], sl=pos["sl"], sup=pos.get("nearest_sup"), res=pos.get("nearest_res"))
                    self._notify(f"🎯 <b>TP Runner поднят</b> · {pair_html(sym, pos)}\n🎯 Новая цель: {fmt_price(pos['tp'])} · 🛡 Стоп: {fmt_price(pos['sl'])}")

        # 2. Проверка неисполненных ордеров (Охота и реквоты)
        for order in list(paper.orders):
            sym = order["symbol"]
            t = tickers.get(sym)
            if not t: continue

            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            if neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Токсичные новости", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 7200)
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · 🤬📰 (⏸️ 2ч)")
                continue

            score_now, candles = await live_score(sym, t, regime, btc_ret, news_items)
            if score_now is None: continue

            closes = [c["close"] for c in candles]
            e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
            a = atr(candles)
            if a <= 0: continue

            order_age = current_time - order["created"]
            entry_mode = order.get("entry_mode", "sniper")
            tier = order.get("tier") or "SMALL"
            is_accum = "accumulation" in order.get("reason_keys", [])
            thr = thrs.get(entry_mode, 6.0)

            # Отмена если цена улетела вверх
            if t["last"] > order["price"] + 2.0 * a: 
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Улетела без нас", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 300)
                self._notify(f"⚠ Снят · {pair_html(sym, order)} · 🚀 Улетела без нас (отмена погони)")
                continue

            if order_age > 7200:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Тайм-аут 2ч", tp=order.get("tp"), sl=order.get("sl"), sup=order.get("nearest_sup"), res=order.get("nearest_res"))
                bot_state.set_cooldown(sym, 420)
                self._notify(f"⚠️ Снят · {pair_html(sym, order)} · ⏳ (⏸️ 7м)")
                continue

            atr_pct = a / t["last"] * 100
            off = entry_offset(score_now, thr, regime, atr_pct, entry_mode, tier=tier, is_accumulation=is_accum)
            ideal_price = t["last"] * (1 + off)
            old_price = order["price"]

            nearest_res, res_touches, nearest_sup, sup_touches = find_sr_levels(candles)
            live_sup = nearest_sup if (nearest_sup and sup_touches >= 2) else order.get("nearest_sup")
            live_sup_touches = sup_touches if (nearest_sup and sup_touches >= 2) else order.get("sup_touches", 2 if order.get("nearest_sup") else 0)
            
            live_res = nearest_res if (nearest_res and res_touches >= 2) else order.get("nearest_res")
            live_res_touches = res_touches if (nearest_res and res_touches >= 2) else order.get("res_touches", 2 if order.get("nearest_res") else 0)

            # ⚡ ПРАВИЛА ОХОТЫ
            action_type = None
            price_icon = ""
            dev_pct = abs(ideal_price - old_price) / old_price * 100

            # 1. Зона заморозки: если цена ближе 0.35 ATR, ордер заблокирован от сдвига вниз!
            in_freeze_zone = abs(t["last"] - old_price) <= (0.35 * a) or (abs(t["last"] - old_price) / old_price * 100) <= 0.35

            # 2. Охота вниз: только плавный пересчет волатильности, не убегая от цены и не уходя под плиту!
            if ideal_price < old_price and dev_pct >= 0.5 and not in_freeze_zone:
                if not (live_sup and ideal_price < live_sup * 1.0015):
                    order["price"] = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"

            # 3. Охота вверх: разрешена ТОЛЬКО в сужении VCP при тихом накоплении!
            elif ideal_price > old_price and dev_pct >= 0.3:
                if is_accum and t["last"] <= (e50 * 1.012):
                    order["price"] = ideal_price
                    action_type = "hunt_vcp"
                    price_icon = "⬆️"

            # Пересчет уровней SL/TP через risk модуль с сохраненными касаниями
            sl, tp, is_valid, _ = calc_initial_sl_tp(
                symbol=sym, entry=order["price"], tier=tier, a=a,
                sup=live_sup, sup_touches=live_sup_touches,
                res=live_res, res_touches=live_res_touches,
                is_mom=(entry_mode == "rocket"), entry_mode=entry_mode
            )

            if is_valid:
                order["sl"] = sl
                order["tp"] = tp

            order["nearest_res"] = live_res
            order["res_touches"] = live_res_touches
                paper.save()
                msg_desc = "подтяжка к сжатию (VCP)" if action_type == "hunt_vcp" else f"сдвиг на {dev_pct:.2f}%"
                paper.log_event(sym, "order_moved", order["price"], f"Сдвиг: {action_type}", tp=order["tp"], sl=order["sl"], sup=live_sup, res=live_res)
                self._notify(f"📐 <b>Сдвиг {price_icon}</b> · {pair_html(sym, order)}\n📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · {msg_desc}")
            else:
                paper.save()
