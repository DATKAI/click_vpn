"""Абстракция транспорта для site-to-site туннелей.

Каждый транспорт (WireGuard, OpenVPN, IPsec, GRE) — плагин, реализующий Transport.
Ядро site-to-site не знает деталей протокола.
"""
from abc import ABC, abstractmethod


class Transport(ABC):
    name: str

    @abstractmethod
    def hub_apply(self, hub_site, spoke_sites: list, is_allowed=None) -> tuple[bool, str]:
        """Поднять/обновить туннели на хабе. Возвращает (ok, message).

        is_allowed — необязательный callable(src_site_id, dst_site_id) -> bool
        (матрица доступа). None = разрешено всё (поведение S1).
        """

    @abstractmethod
    def hub_teardown(self, hub_site) -> None:
        """Остановить и удалить туннели хаба."""

    @abstractmethod
    def site_config(self, hub_site, site, peer_sites: list, is_allowed=None) -> str:
        """Сгенерировать текстовый конфиг для роутера площадки.

        peer_sites — другие площадки (хаб + прочие спицы), чьи LAN-подсети
        должны быть доступны этой спице через туннель (попадают в AllowedIPs).
        is_allowed — callable(src_site_id, dst_site_id) -> bool; LAN площадки
        попадает в AllowedIPs только если доступ к ней разрешён. None = всё.
        """

    @abstractmethod
    def status(self, hub_site) -> list[dict]:
        """Состояние туннелей: [{site_id, pubkey, endpoint, last_handshake, rx, tx, online}]."""

    def link_iface(self, hub_site, spoke) -> str | None:
        """Имя сетевого интерфейса туннеля до спицы (для маршрутного failover).

        None — транспорт без маршрутизируемого интерфейса (напр. policy-based
        IPsec), такой канал нельзя переключать по маршруту.
        """
        return None


_registry: dict[str, Transport] = {}


def register(transport: Transport) -> None:
    _registry[transport.name] = transport


def get(name: str) -> Transport:
    if name not in _registry:
        raise ValueError(f"Транспорт '{name}' не зарегистрирован")
    return _registry[name]


def available() -> list[str]:
    return list(_registry.keys())
