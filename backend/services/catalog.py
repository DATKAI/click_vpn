"""Каталог популярных сервисов для селективной маршрутизации.

Готовые пресеты: сервис → источники (ASN/провайдер). Добавляются в профиль
одним кликом, чтобы не вводить AS-номера вручную. ASN-диапазоны тянутся при
компиляции через RIPEstat.

region: global — зарубежные сервисы, ru — российские.
Источники: ("asn","AS####") | ("provider","cloudflare"|"google") | ("cidr","x/y").
"""

CATALOG = [
    # ── Зарубежные ──────────────────────────────────────────────────────────
    {"id": "google", "name": "Google / YouTube", "category": "Видео и медиа",
     "region": "global", "sources": [("asn", "AS15169"), ("asn", "AS19527"), ("asn", "AS36384")]},
    {"id": "meta", "name": "Instagram / Facebook / WhatsApp", "category": "Соцсети",
     "region": "global", "sources": [("asn", "AS32934")]},
    {"id": "telegram", "name": "Telegram", "category": "Мессенджеры",
     "region": "global", "sources": [("asn", "AS62041"), ("asn", "AS62014"),
                                     ("asn", "AS44907"), ("asn", "AS59930")]},
    {"id": "twitter", "name": "X (Twitter)", "category": "Соцсети",
     "region": "global", "sources": [("asn", "AS13414")]},
    {"id": "linkedin", "name": "LinkedIn", "category": "Соцсети",
     "region": "global", "sources": [("asn", "AS14413")]},
    {"id": "netflix", "name": "Netflix", "category": "Видео и медиа",
     "region": "global", "sources": [("asn", "AS2906"), ("asn", "AS40027")]},
    {"id": "twitch", "name": "Twitch", "category": "Видео и медиа",
     "region": "global", "sources": [("asn", "AS46489")]},
    {"id": "spotify", "name": "Spotify", "category": "Видео и медиа",
     "region": "global", "sources": [("asn", "AS8403"), ("asn", "AS43650")]},
    {"id": "github", "name": "GitHub", "category": "Разработка",
     "region": "global", "sources": [("asn", "AS36459")]},
    {"id": "cloudflare", "name": "Cloudflare (CDN)", "category": "Облака и CDN",
     "region": "global", "sources": [("provider", "cloudflare")]},
    {"id": "aws", "name": "Amazon AWS", "category": "Облака и CDN",
     "region": "global", "sources": [("asn", "AS16509"), ("asn", "AS14618")]},
    {"id": "microsoft", "name": "Microsoft / Azure", "category": "Облака и CDN",
     "region": "global", "sources": [("asn", "AS8075")]},
    {"id": "apple", "name": "Apple", "category": "Облака и CDN",
     "region": "global", "sources": [("asn", "AS714"), ("asn", "AS6185")]},
    {"id": "akamai", "name": "Akamai (CDN)", "category": "Облака и CDN",
     "region": "global", "sources": [("asn", "AS20940"), ("asn", "AS16625")]},

    # ── Российские ──────────────────────────────────────────────────────────
    {"id": "yandex", "name": "Яндекс", "category": "Российские сервисы",
     "region": "ru", "sources": [("asn", "AS13238"), ("asn", "AS200350")]},
    {"id": "vk", "name": "VK / Mail.ru", "category": "Российские сервисы",
     "region": "ru", "sources": [("asn", "AS47541"), ("asn", "AS47542")]},
    {"id": "rostelecom", "name": "Ростелеком", "category": "Российские сервисы",
     "region": "ru", "sources": [("asn", "AS12389")]},
    {"id": "rutube", "name": "RuTube", "category": "Российские сервисы",
     "region": "ru", "sources": [("asn", "AS41549")]},
]


def list_catalog() -> list[dict]:
    return [
        {"id": c["id"], "name": c["name"], "category": c["category"],
         "region": c["region"],
         "sources": [{"kind": k, "value": v} for k, v in c["sources"]]}
        for c in CATALOG
    ]


def get(preset_id: str) -> dict | None:
    for c in CATALOG:
        if c["id"] == preset_id:
            return c
    return None
