"""GRE транспорты для site-to-site.

Два варианта (общий код, выбор через класс):
  • gre        — чистый GRE (Linux ip tunnel mode gre), БЕЗ шифрования.
  • gre_ipsec  — тот же GRE, но защищённый IPsec (transport mode, ESP).

Оба дают маршрутизируемый интерфейс (как WireGuard) — удобно под динамическую
маршрутизацию (BGP/OSPF, этап S5).

ОГРАНИЧЕНИЕ: GRE-эндпоинты статичны → спица должна иметь публичный/статический IP
(Site.endpoint). Для серых IP — WireGuard/IPsec. gre_ipsec добавляет шифрование,
но требование публичного IP остаётся (GRE-туннель привязан к адресам).

На хабе: интерфейс sg{tag}{spoke_id} на спицу, ptp-адресация из туннельной сети
хаба. Скрипты up/down + systemd-юнит click-vpn-s2sgre{tag}-{hub_id}.
gre_ipsec дополнительно: swanctl transport-mode conn (proto gre) + PSK (Site.psk).
"""
import ipaddress
import os
import subprocess

from services.s2s.transport import Transport, register
from services.s2s import netutil

DATA_DIR = os.getenv("DATA_DIR", "./data")
S2S_DIR = os.path.join(DATA_DIR, "s2s")
SWAN_DIR = "/etc/swanctl"

IKE_PROPOSALS = "aes256-sha256-modp2048"
ESP_PROPOSALS = "aes256-sha256-modp2048"


def _iface(tag: str, spoke_id: int) -> str:
    return f"sg{tag}{spoke_id}"   # sg2 / sgi2, ≤ IFNAMSIZ (15)


def _unit_name(tag: str, hub_id: int) -> str:
    return f"click-vpn-s2sgre{tag}-{hub_id}.service"


def _unit_path(tag: str, hub_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(tag, hub_id)}"


def _up_path(tag: str, hub_id: int) -> str:
    return os.path.join(S2S_DIR, f"gre{tag}-{hub_id}-up.sh")


def _down_path(tag: str, hub_id: int) -> str:
    return os.path.join(S2S_DIR, f"gre{tag}-{hub_id}-down.sh")


def _swan_conf_path(hub_id: int) -> str:
    return os.path.join(SWAN_DIR, "conf.d", f"clickvpn-s2sgrei-{hub_id}.conf")


def _host(site) -> str:
    ep = (site.endpoint or "").strip()
    return ep.split(":")[0] if ep else ""


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


def _build_scripts(tag: str, hub_site, gre_spokes: list) -> tuple[str, str]:
    out_if = netutil.default_iface()
    hub_wan = _host(hub_site)
    hub_tip = hub_site.tunnel_ip
    hub_lans = [sn.cidr for sn in hub_site.subnets]

    up = ["#!/bin/sh", "set -e",
          "sysctl -w net.ipv4.ip_forward=1",
          "sysctl -w net.ipv4.conf.all.rp_filter=0"]
    down = ["#!/bin/sh"]

    for spoke in gre_spokes:
        if not spoke.tunnel_ip or not _host(spoke):
            continue
        ifc = _iface(tag, spoke.id)
        rwan = _host(spoke)
        up += [
            f"ip link del {ifc} 2>/dev/null || true",
            f"ip link add {ifc} type gre local {hub_wan} remote {rwan} key {spoke.id} ttl 255",
            f"ip addr add {hub_tip}/32 peer {spoke.tunnel_ip} dev {ifc}",
            f"ip link set {ifc} up",
            f"sysctl -w net.ipv4.conf.{ifc}.rp_filter=0 || true",
        ]
        for sn in spoke.subnets:
            up.append(f"ip route replace {sn.cidr} dev {ifc}")
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


def _create_unit(tag: str, hub_id: int):
    unit = f"""[Unit]
Description=Click VPN Site-to-Site GRE{tag.upper()} hub {hub_id}
After=network-online.target strongswan.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh {_up_path(tag, hub_id)}
ExecStop=/bin/sh {_down_path(tag, hub_id)}

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(tag, hub_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


# ──────────────────── IPsec-обёртка (только gre_ipsec) ────────────────────

def _build_swan_conf(hub_site, gre_spokes: list) -> str:
    """swanctl: transport-mode SA, защищающие GRE между хабом и спицами."""
    hub_wan = _host(hub_site)
    conns, secrets = [], []
    for spoke in gre_spokes:
        rwan = _host(spoke)
        if not rwan or not spoke.psk:
            continue
        name = f"grei-{hub_site.id}-{spoke.id}"
        conns.append(f"""  {name} {{
    version = 2
    proposals = {IKE_PROPOSALS},default
    local_addrs = {hub_wan}
    remote_addrs = {rwan}
    dpd_delay = 30s
    local {{ auth = psk; id = {hub_wan} }}
    remote {{ auth = psk; id = {rwan} }}
    children {{
      gre {{
        mode = transport
        local_ts = {hub_wan}[gre]
        remote_ts = {rwan}[gre]
        start_action = trap
        dpd_action = restart
        esp_proposals = {ESP_PROPOSALS},default
      }}
    }}
  }}
""")
        psk = (spoke.psk or "").replace('"', '\\"')
        secrets.append(f'  ike-{name} {{ id = {rwan} secret = "{psk}" }}')
    return ("connections {\n" + "".join(conns) + "}\n\n"
            + "secrets {\n" + "\n".join(secrets) + "\n}\n")


def _strongswan_service() -> str:
    out = subprocess.run(["systemctl", "list-unit-files"], capture_output=True, text=True).stdout
    for svc in ("strongswan.service", "strongswan-starter.service"):
        if svc in out:
            return svc.replace(".service", "")
    return "strongswan"


def _ensure_strongswan():
    svc = _strongswan_service()
    if subprocess.run(["systemctl", "is-active", "--quiet", svc],
                      capture_output=True).returncode != 0:
        subprocess.run(["systemctl", "enable", svc], capture_output=True)
        subprocess.run(["systemctl", "restart", svc], capture_output=True)
        import time
        time.sleep(2)


# ──────────────────── Генерация конфигов площадки ────────────────────

def _build_mikrotik_script(encrypted: bool, hub_site, spoke, remote_lans: list[str]) -> str:
    host = _host(hub_site)
    net = ipaddress.ip_network(hub_site.tunnel_network, strict=False) if hub_site.tunnel_network else None
    plen = net.prefixlen if net else 32
    title = "GRE over IPsec" if encrypted else "GRE (без шифрования)"
    lines = [f"# === Click VPN site-to-site ({title}) — площадка «{spoke.name}» ==="]
    if encrypted:
        lines += [
            "# IPsec transport-mode для защиты GRE:",
            "/ip ipsec profile add name=clickvpn-grei hash-algorithm=sha256 "
            "enc-algorithm=aes-256 dh-group=modp2048",
            "/ip ipsec proposal add name=clickvpn-grei auth-algorithms=sha256 "
            "enc-algorithms=aes-256-cbc pfs-group=modp2048",
            f'/ip ipsec peer add name=clickvpn-grei address={host}/32 profile=clickvpn-grei exchange-mode=ike2',
            f'/ip ipsec identity add peer=clickvpn-grei auth-method=pre-shared-key secret="{spoke.psk}"',
            f'/ip ipsec policy add peer=clickvpn-grei src-address={_host(spoke)}/32 '
            f'dst-address={host}/32 protocol=gre proposal=clickvpn-grei tunnel=no level=require',
        ]
    lines += [
        f"/interface gre add name=clickvpn-gre remote-address={host} "
        f"local-address={_host(spoke)} keepalive=10s,3",
        f"/ip address add address={spoke.tunnel_ip}/{plen} interface=clickvpn-gre",
    ]
    for rlan in remote_lans:
        lines.append(f"/ip route add dst-address={rlan} gateway=clickvpn-gre")
    return "\n".join(lines) + "\n"


def _build_generic_sheet(encrypted: bool, hub_site, spoke, remote_lans: list[str]) -> str:
    host = _host(hub_site)
    spoke_lans = [sn.cidr for sn in spoke.subnets]
    head = "GRE over IPsec (шифрованный)" if encrypted else "GRE (БЕЗ шифрования)"
    out = (
        f"# Click VPN site-to-site ({head}) — площадка «{spoke.name}»\n"
        f"# Параметры туннеля для роутера филиала:\n\n"
        f"Тип               : GRE (IP protocol 47){' внутри IPsec transport-mode' if encrypted else ', без шифрования'}\n"
        f"Локальный адрес   : {_host(spoke)}  (публичный IP филиала)\n"
        f"Удалённый адрес   : {host}  (хаб)\n"
        f"GRE key           : {spoke.id}\n"
        f"Туннельный IP филиала: {spoke.tunnel_ip}\n"
        f"Туннельный IP хаба   : {hub_site.tunnel_ip}\n"
    )
    if encrypted:
        out += (
            f"\nIPsec (защита GRE):\n"
            f"  Аутентификация : PSK\n"
            f"  PSK            : {spoke.psk}\n"
            f"  Режим          : transport, защищать protocol=gre между {_host(spoke)} и {host}\n"
            f"  IKE            : AES-256, SHA-256, DH14 (modp2048)\n"
            f"  ESP            : AES-256, SHA-256, PFS14\n"
        )
    out += (
        f"\nЛокальные сети (свои): {', '.join(spoke_lans) or '—'}\n"
        f"Маршруты к сетям     : {', '.join(remote_lans) or '—'}  (через GRE-интерфейс)\n"
    )
    if not encrypted:
        out += "\nВНИМАНИЕ: GRE не шифрует трафик — применять на доверенных каналах.\n"
    return out


# ──────────────────── Транспорт ────────────────────

class _BaseGRE(Transport):
    tag = ""
    encrypted = False

    def hub_apply(self, hub_site, spoke_sites: list, is_allowed=None) -> tuple[bool, str]:
        os.makedirs(S2S_DIR, exist_ok=True)
        own = [s for s in spoke_sites if s.transport == self.name]
        up, down = _build_scripts(self.tag, hub_site, own)
        with open(_up_path(self.tag, hub_site.id), "w") as f:
            f.write(up)
        with open(_down_path(self.tag, hub_site.id), "w") as f:
            f.write(down)

        msg = ""
        if self.encrypted:
            # сначала IPsec SA, затем GRE поверх
            os.makedirs(os.path.join(SWAN_DIR, "conf.d"), exist_ok=True)
            with open(_swan_conf_path(hub_site.id), "w") as f:
                f.write(_build_swan_conf(hub_site, own))
            os.chmod(_swan_conf_path(hub_site.id), 0o600)
            _ensure_strongswan()
            sr = subprocess.run(["swanctl", "--load-all"], capture_output=True, text=True)
            if sr.returncode != 0:
                msg = "IPsec: " + (sr.stderr or sr.stdout).strip()

        _create_unit(self.tag, hub_site.id)
        subprocess.run(["systemctl", "enable", _unit_name(self.tag, hub_site.id)], capture_output=True)
        r = subprocess.run(["systemctl", "restart", _unit_name(self.tag, hub_site.id)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        msg = (msg + " " + (r.stderr or r.stdout)).strip()
        return ok, msg or ("OK" if ok else "ошибка поднятия GRE")

    def hub_teardown(self, hub_site) -> None:
        subprocess.run(["systemctl", "disable", "--now", _unit_name(self.tag, hub_site.id)],
                       capture_output=True)
        paths = [_unit_path(self.tag, hub_site.id), _up_path(self.tag, hub_site.id),
                 _down_path(self.tag, hub_site.id)]
        if self.encrypted:
            paths.append(_swan_conf_path(hub_site.id))
        for p in paths:
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        if self.encrypted:
            subprocess.run(["swanctl", "--load-all"], capture_output=True)

    def site_config(self, hub_site, site, peer_sites: list | None = None,
                    is_allowed=None) -> str:
        all_sites = (peer_sites or []) + [site]
        remote_lans = _reachable_lans(site.id, all_sites, is_allowed)
        sheet = _build_generic_sheet(self.encrypted, hub_site, site, remote_lans)
        mt = _build_mikrotik_script(self.encrypted, hub_site, site, remote_lans)
        return (sheet + "\n" + "=" * 60 + "\n"
                + "# Вариант для MikroTik (RouterOS) — команды:\n\n" + mt)

    def status(self, hub_site) -> list[dict]:
        import re
        r = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True)
        present = r.stdout if r.returncode == 0 else ""
        out = []
        pat = rf"\bsg{self.tag}(\d+)[@:]" if self.tag else r"\bsg(\d+)[@:]"
        for m in re.finditer(pat, present):
            out.append({"spoke_id": int(m.group(1)), "online": True})
        return out


class GRETransport(_BaseGRE):
    name = "gre"
    tag = ""
    encrypted = False


class GREIPsecTransport(_BaseGRE):
    name = "gre_ipsec"
    tag = "i"
    encrypted = True


register(GRETransport())
register(GREIPsecTransport())
