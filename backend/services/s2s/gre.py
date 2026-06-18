"""GRE транспорт для site-to-site (без шифрования).

Простой L3-туннель (Linux ip tunnel mode gre). Даёт маршрутизируемый интерфейс,
как WireGuard, но БЕЗ шифрования — трафик идёт открытым. Применять только на
доверенных каналах или когда шифрование обеспечено отдельно.

ОГРАНИЧЕНИЕ: GRE плохо проходит через NAT (нет портов). Спица должна иметь
публичный/статический IP (Site.endpoint). Для серых IP — WireGuard/IPsec.

На хабе: один интерфейс GRE на спицу (sg{spoke_id}), адресация ptp из туннельной
сети хаба (как у WG). Скрипт up/down + systemd-юнит click-vpn-s2sgre-{hub_id}.
"""
import ipaddress
import os
import subprocess

from services.s2s.transport import Transport, register
from services.s2s import netutil

DATA_DIR = os.getenv("DATA_DIR", "./data")
S2S_DIR = os.path.join(DATA_DIR, "s2s")


def _iface(spoke_id: int) -> str:
    return f"sg{spoke_id}"   # ≤ IFNAMSIZ (15)


def _unit_name(hub_id: int) -> str:
    return f"click-vpn-s2sgre-{hub_id}.service"


def _unit_path(hub_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(hub_id)}"


def _up_path(hub_id: int) -> str:
    return os.path.join(S2S_DIR, f"gre-{hub_id}-up.sh")


def _down_path(hub_id: int) -> str:
    return os.path.join(S2S_DIR, f"gre-{hub_id}-down.sh")


def _hub_host(hub_site) -> str:
    ep = (hub_site.endpoint or "").strip()
    return ep.split(":")[0] if ep else ""


def _spoke_host(spoke) -> str:
    ep = (spoke.endpoint or "").strip()
    return ep.split(":")[0] if ep else ""


def _build_scripts(hub_site, gre_spokes: list) -> tuple[str, str]:
    """Возвращает (up_script, down_script) для GRE-туннелей хаба."""
    out_if = netutil.default_iface()
    hub_wan = _hub_host(hub_site)
    hub_tip = hub_site.tunnel_ip
    hub_lans = [sn.cidr for sn in hub_site.subnets]

    up = ["#!/bin/sh", "set -e",
          "sysctl -w net.ipv4.ip_forward=1",
          "sysctl -w net.ipv4.conf.all.rp_filter=0"]
    down = ["#!/bin/sh"]

    for spoke in gre_spokes:
        if not spoke.tunnel_ip or not _spoke_host(spoke):
            continue
        ifc = _iface(spoke.id)
        rwan = _spoke_host(spoke)
        up += [
            f"ip link del {ifc} 2>/dev/null || true",
            f"ip link add {ifc} type gre local {hub_wan} remote {rwan} key {spoke.id} ttl 255",
            f"ip addr add {hub_tip}/32 peer {spoke.tunnel_ip} dev {ifc}",
            f"ip link set {ifc} up",
            f"sysctl -w net.ipv4.conf.{ifc}.rp_filter=0 || true",
        ]
        for sn in spoke.subnets:
            up.append(f"ip route replace {sn.cidr} dev {ifc}")
        # NAT подсетей спицы при выходе в LAN хаба (для доступа к сети хаба)
        for slan in [sn.cidr for sn in spoke.subnets]:
            for hlan in hub_lans:
                up.append(f"iptables -t nat -C POSTROUTING -s {slan} -d {hlan} -o {out_if} -j MASQUERADE 2>/dev/null || "
                          f"iptables -t nat -A POSTROUTING -s {slan} -d {hlan} -o {out_if} -j MASQUERADE")
        up += [
            f"iptables -C FORWARD -i {ifc} -j ACCEPT 2>/dev/null || iptables -A FORWARD -i {ifc} -j ACCEPT",
            f"iptables -C FORWARD -o {ifc} -j ACCEPT 2>/dev/null || iptables -A FORWARD -o {ifc} -j ACCEPT",
        ]
        down.append(f"ip link del {ifc} 2>/dev/null || true")

    return "\n".join(up) + "\n", "\n".join(down) + "\n"


def _create_unit(hub_id: int):
    unit = f"""[Unit]
Description=Click VPN Site-to-Site GRE hub {hub_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh {_up_path(hub_id)}
ExecStop=/bin/sh {_down_path(hub_id)}

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(hub_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def _build_mikrotik_script(hub_site, spoke, remote_lans: list[str]) -> str:
    host = _hub_host(hub_site)
    net = ipaddress.ip_network(hub_site.tunnel_network, strict=False) if hub_site.tunnel_network else None
    plen = net.prefixlen if net else 32
    lines = [
        f"# === Click VPN site-to-site (GRE, без шифрования) — площадка «{spoke.name}» ===",
        "# Вставить в терминал MikroTik (RouterOS).",
        f"/interface gre add name=clickvpn-gre remote-address={host} "
        f"local-address={_spoke_host(spoke)} keepalive=10s,3",
        f"/ip address add address={spoke.tunnel_ip}/{plen} interface=clickvpn-gre",
    ]
    for rlan in remote_lans:
        lines.append(f"/ip route add dst-address={rlan} gateway=clickvpn-gre")
    return "\n".join(lines) + "\n"


def _build_generic_sheet(hub_site, spoke, remote_lans: list[str]) -> str:
    host = _hub_host(hub_site)
    spoke_lans = [sn.cidr for sn in spoke.subnets]
    return (
        f"# Click VPN site-to-site (GRE, БЕЗ шифрования) — площадка «{spoke.name}»\n"
        f"# Параметры GRE-туннеля для роутера филиала:\n\n"
        f"Тип               : GRE (IP protocol 47), без шифрования\n"
        f"Локальный адрес   : {_spoke_host(spoke)}  (публичный IP филиала)\n"
        f"Удалённый адрес   : {host}  (хаб)\n"
        f"GRE key           : {spoke.id}\n"
        f"Туннельный IP филиала: {spoke.tunnel_ip}\n"
        f"Туннельный IP хаба   : {hub_site.tunnel_ip}\n\n"
        f"Локальные сети (свои): {', '.join(spoke_lans) or '—'}\n"
        f"Маршруты к сетям     : {', '.join(remote_lans) or '—'}  (через GRE-интерфейс)\n\n"
        f"ВНИМАНИЕ: GRE не шифрует трафик и плохо работает за NAT — нужен публичный IP филиала.\n"
    )


def _reachable_lans(site_id: int, all_sites: list, is_allowed=None) -> list[str]:
    out = []
    for s in all_sites:
        if s.id == site_id:
            continue
        if is_allowed is not None and not is_allowed(site_id, s.id):
            continue
        for sn in s.subnets:
            if sn.cidr not in out:
                out.append(sn.cidr)
    return out


class GRETransport(Transport):
    name = "gre"

    def hub_apply(self, hub_site, spoke_sites: list, is_allowed=None) -> tuple[bool, str]:
        os.makedirs(S2S_DIR, exist_ok=True)
        gre_spokes = [s for s in spoke_sites if s.transport == "gre"]
        up, down = _build_scripts(hub_site, gre_spokes)
        with open(_up_path(hub_site.id), "w") as f:
            f.write(up)
        with open(_down_path(hub_site.id), "w") as f:
            f.write(down)
        _create_unit(hub_site.id)
        subprocess.run(["systemctl", "enable", _unit_name(hub_site.id)], capture_output=True)
        r = subprocess.run(["systemctl", "restart", _unit_name(hub_site.id)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        msg = (r.stderr or r.stdout).strip()
        return ok, msg or ("OK" if ok else "ошибка поднятия GRE")

    def hub_teardown(self, hub_site) -> None:
        subprocess.run(["systemctl", "disable", "--now", _unit_name(hub_site.id)],
                       capture_output=True)
        for p in (_unit_path(hub_site.id), _up_path(hub_site.id), _down_path(hub_site.id)):
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "daemon-reload"], check=False)

    def site_config(self, hub_site, site, peer_sites: list | None = None,
                    is_allowed=None) -> str:
        all_sites = (peer_sites or []) + [site]
        remote_lans = _reachable_lans(site.id, all_sites, is_allowed)
        mt = _build_mikrotik_script(hub_site, site, remote_lans)
        sheet = _build_generic_sheet(hub_site, site, remote_lans)
        return (sheet + "\n" + "=" * 60 + "\n"
                + "# Вариант для MikroTik (RouterOS) — команды:\n\n" + mt)

    def status(self, hub_site) -> list[dict]:
        # GRE без состояния: «онлайн» = интерфейс sgN существует (поднят).
        import re
        r = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True)
        present = r.stdout if r.returncode == 0 else ""
        out = []
        for m in re.finditer(r"\bsg(\d+)[@:]", present):
            out.append({"spoke_id": int(m.group(1)), "online": True})
        return out


register(GRETransport())
