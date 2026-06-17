"""Абстракция транспорта для site-to-site туннелей.

Каждый транспорт (WireGuard, OpenVPN, IPsec, GRE) — плагин, реализующий Transport.
Ядро site-to-site не знает деталей протокола.
"""
from abc import ABC, abstractmethod


class Transport(ABC):
    name: str

    @abstractmethod
    def hub_apply(self, hub_site, spoke_sites: list) -> tuple[bool, str]:
        """Поднять/обновить туннели на хабе. Возвращает (ok, message)."""

    @abstractmethod
    def hub_teardown(self, hub_site) -> None:
        """Остановить и удалить туннели хаба."""

    @abstractmethod
    def site_config(self, hub_site, site) -> str:
        """Сгенерировать текстовый конфиг для роутера площадки."""

    @abstractmethod
    def status(self, hub_site) -> list[dict]:
        """Состояние туннелей: [{site_id, pubkey, endpoint, last_handshake, rx, tx, online}]."""


_registry: dict[str, Transport] = {}


def register(transport: Transport) -> None:
    _registry[transport.name] = transport


def get(name: str) -> Transport:
    if name not in _registry:
        raise ValueError(f"Транспорт '{name}' не зарегистрирован")
    return _registry[name]


def available() -> list[str]:
    return list(_registry.keys())
