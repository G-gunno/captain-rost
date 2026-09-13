def _parse(xml_text):
    items = []
    
    # Попытка 1: строгий XML парсер
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter('item'):
            items.append({
                'title': item.findtext('title') or '',
                'desc': item.findtext('description') or '',
                'link': item.findtext('link') or '',
                'ts': int(time.time()),
            })
        return items
    except ET.ParseError:
        pass  # Сломанный XML, идем в regex
    except Exception as e:
        logger.debug(f"RSS XML parse error: {e}")

    # Попытка 2: Бронебойный Regex (Fallback)
    try:
        item_pattern = re.compile(r'<item[^>]*>(.*?)</item>', re.IGNORECASE | re.DOTALL)
        for block in item_pattern.finditer(xml_text):
            chunk = block.group(1)
            title_m = re.search(r'<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>', chunk, re.IGNORECASE | re.DOTALL)
            desc_m = re.search(r'<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>', chunk, re.IGNORECASE | re.DOTALL)
            link_m = re.search(r'<link[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>', chunk, re.IGNORECASE | re.DOTALL)
            items.append({
                'title': title_m.group(1).strip() if title_m else '',
                'desc': desc_m.group(1).strip() if desc_m else '',
                'link': link_m.group(1).strip() if link_m else '',
                'ts': int(time.time()),
            })
    except Exception as e:
        logger.error(f"RSS regex fallback error: {e}")

    return items
