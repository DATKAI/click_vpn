"""Реестр управляемых модулей (расширений панели).

Модули включаются/выключаются из UI. Каждый — отдельная функциональность
поверх ядра (биллинг, и т.д.). Ядро остаётся чистым; модуль добавляет свои
поля/логику и активируется флагом.
"""

# Каталог доступных модулей (метаданные для UI)
REGISTRY = {
    "billing": {
        "title": "Биллинг и тарифы",
        "description": "Продажа VPN-доступа: тарифы, баланс, лимиты трафика и срока, "
                       "автоблокировка при неоплате или превышении лимита.",
        "icon": "billing",
    },
}


def is_enabled(db, name: str) -> bool:
    from models import Module
    m = db.query(Module).filter(Module.name == name).first()
    return bool(m and m.enabled)


def seed(db):
    """Создаёт записи модулей из реестра (выключенными), если их ещё нет."""
    from models import Module
    for name in REGISTRY:
        if not db.query(Module).filter(Module.name == name).first():
            db.add(Module(name=name, enabled=False))
    db.commit()


def list_modules(db) -> list[dict]:
    from models import Module
    rows = {m.name: m for m in db.query(Module).all()}
    out = []
    for name, meta in REGISTRY.items():
        m = rows.get(name)
        out.append({
            "name": name,
            "title": meta["title"],
            "description": meta["description"],
            "icon": meta.get("icon"),
            "enabled": bool(m and m.enabled),
        })
    return out
