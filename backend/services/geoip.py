"""Определение страны по IP через бесплатный ip-api.com (без зависимостей/баз).

Резолвятся только IP сканеров/ботов (публичные), не клиенты — приватность
не затрагивается. Лимит ip-api.com ~45 запросов/мин, поэтому backfill идёт
порциями в фоне.
"""
import json
import urllib.request


def lookup(ip: str):
    """Возвращает (country, country_code) или (None, None)."""
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode"
        with urllib.request.urlopen(url, timeout=4) as r:
            d = json.loads(r.read().decode())
        if d.get("status") == "success":
            return d.get("country"), d.get("countryCode")
    except Exception:
        pass
    return None, None
