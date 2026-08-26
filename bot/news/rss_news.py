import re
import time
import xml.etree.ElementTree as ET

import httpx
from loguru import logger

_cache = {"items": None, "ts": 0, "listings": None, "listings_ts": 0}

NEWS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
]

NEG_WORDS = [
    "hack", "hacked", "hacking", "exploit", "exploited", "vulnerability",
    "rug pull", "rugpull", "scam", "fraud", "delist", "delisting",
    "lawsuit", "sued", "sec investigation", "ban", "banned",
    "bankrupt", "bankruptcy", "insolvent", "collapse", "collapsed",
    "ponzi", "pyramid", "exit scam", "fake", "fake volume"
]

POS_WORDS = [
    "partnership", "partnerships", "integration", "integrated",
    "etf approval", "etf approved", "listing", "listed", "launch", "launched",
    "adoption", "adopted", "institutional", "bullish", "breakout",
    "ath", "all time high", "record high", "surge", "soaring"
]


def _count(words, text):
    """Считает совпадения по границам слов (bank != ban)."""
    total = 0
    for w in words:
        pat = re.compile(r'\b' + re.escape(w) + r'\b', re.IGNORECASE)
        if pat.search(text):
            total += 1
    return total


def _parse(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter('item'):
            items.append({
                'title': item.findtext('title') or '',
                'desc': item.findtext('description') or '',
                'link': item.findtext('link') or '',
                'ts': int(time.time()),
            })
    except Exception as e:
        logger.error(f"RSS parse error: {e}")
    return items


def _parse_listings(xml_text):
    """Извлекает новые листинги из RSS Bybit — устойчиво к невалидному XML."""
    listings = []

    # Попытка 1: стандартный XML-парсер
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter('item'):
            title = item.findtext('title') or ''
            link = item.findtext('link') or ''
            pub_date = item.findtext('pubDate')
            entry = _extract_listing(title, link, pub_date)
            if entry:
                listings.append(entry)
        return listings
    except ET.ParseError:
        pass  # невалидный XML — идём в fallback
    except Exception as e:
        logger.debug(f"Listings ET parse failed, trying regex fallback: {e}")

    # Попытка 2: regex-fallback — работает с битым XML/HTML
    try:
        item_pattern = re.compile(
            r'<item[^>]*>(.*?)</item>',
            re.IGNORECASE | re.DOTALL
        )
        for block in item_pattern.finditer(xml_text):
            chunk = block.group(1)
            title_m = re.search(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>',
                                chunk, re.IGNORECASE | re.DOTALL)
            link_m = re.search(r'<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>',
                               chunk, re.IGNORECASE | re.DOTALL)
            date_m = re.search(r'<pubDate[^>]*>(.*?)</pubDate>',
                               chunk, re.IGNORECASE | re.DOTALL)
            title = title_m.group(1).strip() if title_m else ''
            link = link_m.group(1).strip() if link_m else ''
            pub_date = date_m.group(1).strip() if date_m else None
            entry = _extract_listing(title, link, pub_date)
            if entry:
                listings.append(entry)
        if listings:
            logger.info(f"Listings parsed via regex fallback: {len(listings)} items")
    except Exception as e:
        logger.error(f"Listings regex fallback also failed: {e}")

    return listings


def _extract_listing(title, link, pub_date):
    """Если заголовок про новый листинг — возвращает запись, иначе None."""
    match = re.search(r'New Listing:?\s+([A-Z0-9]+)\s*USDT', title, re.IGNORECASE)
    if not match:
        match = re.search(r'\b([A-Z0-9]{2,10})\s*USDT\s+(?:Now\s+)?Listed', title, re.IGNORECASE)
    if not match:
        return None
    symbol = match.group(1).upper() + 'USDT'
    ts = int(time.time())
    if pub_date:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub_date)
            ts = int(dt.timestamp())
        except Exception:
            pass
    return {'symbol': symbol, 'title': title.strip(), 'link': (link or '').strip(), 'ts': ts}


async def fetch_news_cache():
    """Свежие заголовки из RSS (кэш 15 минут)."""
    now = time.time()
    if _cache["items"] is not None and now - _cache["ts"] < 900:
        return _cache["items"]
    all_items = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        for url in NEWS_FEEDS:
            try:
                r = await c.get(url)
                all_items += _parse(r.text)
            except Exception as e:
                logger.error(f"RSS fetch error {url}: {e}")
    _cache["items"], _cache["ts"] = all_items, now
    return all_items


async def fetch_listings_cache():
    """Новые листинги Bybit (кэш 1 час)."""
    now = time.time()
    if _cache["listings"] is not None and now - _cache["listings_ts"] < 3600:
        return _cache["listings"]
    listings = []
    for url in [
        "https://announcements.bybit.com/en/rss",
        "https://announcements.bybit.com/rss",
    ]:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url)
            found = _parse_listings(r.text)
            if found:
                listings = found
                break
        except Exception as e:
            logger.error(f"Listings fetch error {url}: {e}")
    _cache["listings"], _cache["listings_ts"] = listings, now
    return listings


def check_sentiment(items, keys):
    """Новостной фон по монете: (негатив, позитив, упоминания, заголовки)."""
    neg = pos = mentions = 0
    heads = []
    keys_low = [k.lower() for k in keys if k]
    for it in items:
        text = it["title"] + " " + it["desc"]
        if not any(k in text.lower() for k in keys_low):
            continue
        mentions += 1
        n = _count(NEG_WORDS, text)
        p = _count(POS_WORDS, text)
        if n > p:
            neg += 1
            heads.append("⚠️ " + it["title"])
        elif p > n:
            pos += 1
            heads.append("✅ " + it["title"])
    return neg, pos, mentions, heads[:2]


def get_stats():
    """Статистика RSS-лент для мониторинга."""
    now = time.time()
    cache_age = now - _cache["ts"] if _cache["ts"] else None
    items = _cache["items"] or []
    listings = _cache["listings"] or []

    neg_examples = []
    pos_examples = []
    for item in items[:20]:
        text = item["title"] + " " + item["desc"]
        n = _count(NEG_WORDS, text)
        p = _count(POS_WORDS, text)
        if n > p and len(neg_examples) < 3:
            neg_examples.append(item["title"][:80])
        elif p > n and len(pos_examples) < 3:
            pos_examples.append(item["title"][:80])

    now_ts = int(time.time())
    fresh_listings = [
        l for l in listings
        if 86400 <= now_ts - l['ts'] <= 1209600  # 24ч – 14 дней
    ]

    return {
        "cache_age_min": round(cache_age / 60, 1) if cache_age else None,
        "items_count": len(items),
        "feeds_working": len(items) > 0,
        "neg_examples": neg_examples,
        "pos_examples": pos_examples,
        "listings_count": len(fresh_listings),
        "listings_recent": [l['symbol'] for l in fresh_listings[:10]],
    }
