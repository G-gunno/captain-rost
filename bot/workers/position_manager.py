import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import live_score, threshold
from bot.strategy.indicators import atr, ema, rsi
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.sizing import entry_offset
from bot.utils.format import pair_html, usd, fmt_price, fmt_pct, corr_txt, funding_line, pnl_emoji
from bot.core.state import bot_state

class PositionManagerWorker:
    """Медленный I/O воркер: проверяет новости, инвалидацию скора, двигает ордера-снайперы за ценой."""
    
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.regime_queue = self.bus.subscribe("REGIME_UPDATED", maxsize=2)
        self.current_regime = "neutral"
        self._fomo_cooldowns = {}
        self.FEE_PCT = 0.10
        self.MIN_TP_PCT = 0.60
        self.MIN_SL_PCT = 0.35

    async def run(self):
        logger.info("🛡 Position Manager запущен: охрана позиций и реквоты ордеров...")
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
                
            await asyncio.sleep(60) # Проверяем здоровье раз в минуту

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

        # Обновление метрик 24ч
        metrics_24h = paper.get_metrics(tickers, hours=24)
        learner.update_threshold(metrics_24h["profit_factor"], metrics_24h["max_drawdown_pct"], metrics_24h["total_trades"])

        news_items = await fetch_news_cache()
        regime = self.current_regime
        thr = threshold(regime)
        current_time = int(time.time())

        # 1. Проверка ОТКРЫТЫХ ПОЗИЦИЙ
        for sym, pos in list(paper.positions.items()):
            t = tickers.get(sym)
            if not t: continue
            
            score_pos, candles = await live_score(sym, t, regime, news_items, deriv_t=deriv_tickers.get(sym))
            if score_pos is None: continue
            
            closes = [c["close"] for c in candles]
            a = atr(candles)
            if a <= 0: continue
            
            last = t["last"]
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
            trend_broken = last < e50 and e21 < e50
            
            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            is_toxic = neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33)

            # Выход по новостям (Новостная резка)
            if is_toxic:
                self._fomo_cooldowns[sym] = current_time + 7200
                if pnl_pct >= 1.0 or pnl_pct <= 0:
                    reason = "✂️📰 Новости"
                    ex = paper._sell(sym, last, reason, regime_now=regime)
                    paper.log_event(sym, "sell", last, f"Новости {neg}/{mentions}")
                    self._notify(
                        f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason} {neg}/{mentions}\n"
                        f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                    )
                    continue

            signal_weak = trend_broken or score_pos <= thr - 2
            pos_corr = pos.get("corr", 0.5)
            regime_danger = (pos.get("regime_entry") == "bull" and pos_corr >= 0.45 and (regime == "bear" or (regime == "neutral" and score_pos < thr)))

            # Инвалидация
            if signal_weak or regime_danger:
                self._fomo_cooldowns[sym] = current_time + 7200
                if regime_danger and not signal_weak:
                    reason = "⚠️ Режим"
                elif pnl_pct > 0:
                    reason = "🪫 Ослаб"
                else:
                    reason = "✂️📉 Резка убытка"

                ex = paper._sell(sym, last, reason, regime_now=regime)
                paper.log_event(sym, "sell", last, reason)
                if ex["pnl"] > 0: shadow.mark_success(sym)
                self._notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym, ex)} · {reason}\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])}"
                )
                continue

            # Раннер: подъем TP
            if pos.get("tp1_done") and last > e21 > e50 and score_pos >= thr and last >= pos["tp"] - 0.3 * a:
                new_tp = max(pos["tp"], last + 1.5 * a)
                if new_tp > pos["tp"]:
                    pos["tp"] = round(new_tp, 10)
                    pos["sl"] = max(pos["sl"], pos["avg"] + 0.5 * a)
                    paper.save()
                    self._notify(f"🎯 <b>TP поднят</b> (раннер) · {pair_html(sym, pos)}\n🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])}")

        # 3. Проверка ВЫХОДОВ по лимиткам (TP/SL)
        for ex in paper.check_exits(tickers, regime_now=regime):
            self._fomo_cooldowns[ex["symbol"]] = current_time + 7200
            paper.log_event(ex["symbol"], "sell", ex["price"], ex["reason"])

            if ex["pnl"] > 0:
                shadow.mark_success(ex["symbol"])

            runner_txt = ""
            if ex.get("runner_bonus", 0) > 5:
                runner_txt = f"\n🏃 пробежка +{ex['runner_bonus']:.1f}% выше TP1"

            ind = "🔥" if ex.get("exit_type") == "TP1_RUN" else pnl_emoji(ex["pnl_pct"])
            self._notify(
                f"💸 <b>Продажа</b> · {pair_html(ex['symbol'], ex)} · {ex['reason']}\n"
                f"{ind} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}{runner_txt}"
                f"{funding_line(ex.get('transferred', 0))}"
            )

        # 4. Проверка ОРДЕРОВ (реквоты и отмены)
        for order in list(paper.orders):
            sym = order["symbol"]
            t = tickers.get(sym)
            if not t: continue
            
            base = sym[:-4]
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])

            if neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Токсичные новости")
                self._fomo_cooldowns[sym] = current_time + 7200
                self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · 🤬📰 {neg}/{mentions} (⏸️ 2ч)")
                continue

            score_now, candles = await live_score(sym, t, regime, news_items)
            if score_now is None: continue
            
            if score_now <= thr - 1.5:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Сигнал умер")
                self._fomo_cooldowns[sym] = current_time + 7200
                self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · ☠️ (⏸️ 2ч)")
                continue
                
            if score_now < thr - 0.5:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Сигнал ослаб")
                self._fomo_cooldowns[sym] = current_time + 1800
                self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · 🪫 (⏸️ 30м)")
                continue

            if (current_time - order["created"]) > 7200:
                paper.cancel_order(order["id"])
                paper.log_event(sym, "cancel", t["last"], "Тайм-аут 2ч")
                self._fomo_cooldowns[sym] = current_time + 1800
                self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · ⏳ (⏸️ 30м)")
                continue

            # Реквот (сдвиг за ценой)
            a = atr(candles)
            if a <= 0: continue
            
            atr_pct = a / t["last"] * 100
            entry_mode = order.get("entry_mode", "sniper")
            off = entry_offset(score_now, thr, regime, atr_pct, entry_mode)
            
            ideal_price = t["last"] * (1 + off)
            old_price = order["price"]
            dev_pct = abs(ideal_price - old_price) / old_price * 100
            
            action_type = None

            if entry_mode in ("rocket", "reversal"):
                if ideal_price > old_price and dev_pct >= 0.2:
                    if order.get("hunt_count", 0) >= 2:
                        paper.cancel_order(order["id"])
                        paper.log_event(sym, "cancel", t["last"], "3 попытки догнать")
                        self._fomo_cooldowns[sym] = current_time + 1800
                        self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · 🏃 (⏸️ 30м)")
                        continue
                    order["price"] = ideal_price
                    order["hunt_count"] = order.get("hunt_count", 0) + 1
                    action_type = "hunt"
                    price_icon = "⬆️"
                elif ideal_price < old_price and dev_pct >= 0.2:
                    order["price"] = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"
            else:
                ideal_price = min(ideal_price, t.get("bid1", t["last"]))
                if t["last"] > old_price + 1.5 * a:
                    paper.cancel_order(order["id"])
                    paper.log_event(sym, "cancel", t["last"], "Улетела без нас")
                    self._fomo_cooldowns[sym] = current_time + 1800
                    self._notify(f"⚠️ <b>Снят</b> · {pair_html(sym, order)} · 🚀 (⏸️ 30м)")
                    continue
                if ideal_price < old_price and dev_pct >= 0.2:
                    order["price"] = ideal_price
                    action_type = "correct"
                    price_icon = "⬇️"

            order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + self.MIN_TP_PCT / 100))
            order["sl"] = min(order["price"] - 1.2 * a, order["price"] * (1 - self.MIN_SL_PCT / 100))
            
            if action_type:
                order["created"] = current_time  
                paper.save()
                paper.log_event(sym, "order_moved", ideal_price, f"Сдвиг: {action_type}")
                if action_type == "hunt":
                    msg_title = f"🏃 <b>Охота {price_icon}</b>"
                    msg_desc = f"попытка {order['hunt_count']}/3"
                else:
                    msg_title = f"📐 <b>Коррекция {price_icon}</b>"
                    msg_desc = f"сдвиг на {dev_pct:.2f}%"

                self._notify(
                    f"{msg_title} · {pair_html(sym, order)}\n"
                    f"📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · {msg_desc}"
                )
            else:
                paper.save()
