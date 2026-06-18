"""WireGuard транспорт для site-to-site.

Поднимает отдельный WG-интерфейс s2s{hub_id} на хабе.
Конфиги: /var/lib/click-vpn/s2s/s2s{id}.conf
Systemd: click-vpn-s2s-{id}.service
"""
import ipaddress
import os
import subprocess
import time

from services.s2s.transport import Transport, register

DATA_DIR = os.getenv("DATA_DIR", "./data")
S2S_DIR = os.path.join(DATA_DIR, "s2s")


def _iface(hub_id: int) -> str:
    return f"s2s{hub_id}"


def _conf_path(hub_id: int) -> str:
    return os.path.join(S2S_DIR, f"s2s{hub_id}.conf")


def _unit_name(hub_id: int) -> str:
    return f"click-vpn-s2s-{hub_id}.service"


def _unit_path(hub_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(hub_id)}"


def _default_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"],
                                      stderr=subprocess.DEVNULL, text=True).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


def gen_keypair() -> tuple[str, str]:
    priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True).stdout.strip()
    return priv, pub


def _hub_tunnel_ip(tunnel_network: str) -> str:
    net = ipaddress.ip_network(tunnel_network, strict=False)
    return str(list(net.hosts())[0])


def _next_spoke_ip(tunnel_network: str, used_ips: list[str]) -> str:
    net = ipaddress.ip_network(tunnel_network, strict=False)
    hosts = list(net.hosts())
    hub_ip = str(hosts[0])
    used = set(used_ips) | {hub_ip}
    for h in hosts[1:]:
        if str(h) not in used:
            return str(h)
    raise RuntimeError("Нет свободных адресов в туннельной сети")


def _build_hub_conf(hub_site, spoke_sites: list) -> str:
    net = ipaddress.ip_network(hub_site.tunnel_network, strict=False)
    hub_ip = hub_site.tunnel_ip or _hub_tunnel_ip(hub_site.tunnel_network)
    iface = _default_iface()

    # Источники, которые надо маскарадить при выходе в LAN хаба (default iface):
    # туннельная сеть + LAN всех спиц. Иначе хосты в LAN хаба не знают обратного
    # маршрута к подсетям филиалов и доступ к сети хаба не работает «из коробки».
    masq_srcs = [hub_site.tunnel_network]
    for spoke in spoke_sites:
        for sn in spoke.subnets:
            if sn.cidr not in masq_srcs:
                masq_srcs.append(sn.cidr)

    up = ["sysctl -w net.ipv4.ip_forward=1",
          "iptables -A FORWARD -i %i -j ACCEPT", "iptables -A FORWARD -o %i -j ACCEPT"]
    down = ["iptables -D FORWARD -i %i -j ACCEPT", "iptables -D FORWARD -o %i -j ACCEPT"]
    for src in masq_srcs:
        up.append(f"iptables -t nat -A POSTROUTING -s {src} -o {iface} -j MASQUERADE")
        down.append(f"iptables -t nat -D POSTROUTING -s {src} -o {iface} -j MASQUERADE")

    lines = [
        "[Interface]",
        f"Address = {hub_ip}/{net.prefixlen}",
        f"ListenPort = {hub_site.tunnel_port}",
        f"PrivateKey = {hub_site.wg_private_key}",
        f"PostUp = {'; '.join(up)}",
        f"PostDown = {'; '.join(down)}",
        "",
    ]
    for spoke in spoke_sites:
        if not spoke.wg_public_key or not spoke.tunnel_ip:
            continue
        allowed = [f"{spoke.tunnel_ip}/32"]
        for sn in spoke.subnets:
            allowed.append(sn.cidr)
        lines += [
            f"# {spoke.name}",
            "[Peer]",
            f"PublicKey = {spoke.wg_public_key}",
            f"AllowedIPs = {', '.join(allowed)}",
            "PersistentKeepalive = 25",
            "",
        ]
    return "\n".join(lines) + "\n"


def _build_spoke_conf(hub_site, spoke, peer_sites: list | None = None) -> str:
    """Конфиг для роутера площадки-спицы.

    peer_sites — другие площадки (хаб + прочие спицы), чьи LAN должны быть
    доступны этой спице через хаб. Их подсети идут в AllowedIPs, иначе
    WireGuard не направит трафик к ним в туннель.
    """
    net = ipaddress.ip_network(hub_site.tunnel_network, strict=False)

    # AllowedIPs спицы: туннельная сеть + LAN хаба + LAN остальных спиц.
    # (S1: разрешаем все известные подсети; матрица доступа — в S2)
    allowed = [hub_site.tunnel_network]
    seen = {hub_site.tunnel_network}
    for site in (peer_sites or []):
        if site.id == spoke.id:
            continue
        for sn in site.subnets:
            if sn.cidr not in seen:
                allowed.append(sn.cidr)
                seen.add(sn.cidr)

    lines = [
        f"# WireGuard конфиг площадки «{spoke.name}»",
        "# Применить на роутере филиала",
        "",
        "[Interface]",
        f"PrivateKey = {spoke.wg_private_key}",
        f"Address = {spoke.tunnel_ip}/{net.prefixlen}",
        "",
        "[Peer]",
        f"PublicKey = {hub_site.wg_public_key}",
        f"Endpoint = {hub_site.endpoint}",
        f"AllowedIPs = {', '.join(allowed)}",
        "PersistentKeepalive = 25",
    ]
    return "\n".join(lines) + "\n"


def _create_unit(hub_id: int):
    conf = _conf_path(hub_id)
    unit = f"""[Unit]
Description=Click VPN Site-to-Site hub {hub_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/wg-quick up {conf}
ExecStop=/usr/bin/wg-quick down {conf}

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(hub_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def _is_running(hub_id: int) -> bool:
    r = subprocess.run(["wg", "show", _iface(hub_id)], capture_output=True)
    return r.returncode == 0


class WireGuardTransport(Transport):
    name = "wireguard"

    def hub_apply(self, hub_site, spoke_sites: list) -> tuple[bool, str]:
        os.makedirs(S2S_DIR, exist_ok=True)
        conf = _build_hub_conf(hub_site, spoke_sites)
        path = _conf_path(hub_site.id)
        with open(path, "w") as f:
            f.write(conf)
        os.chmod(path, 0o600)

        if _is_running(hub_site.id):
            # syncconf: обновить пиров без перезапуска
            r = subprocess.run(
                ["bash", "-c", f"wg syncconf {_iface(hub_site.id)} <(wg-quick strip {path})"],
                capture_output=True, text=True,
            )
        else:
            _create_unit(hub_site.id)
            subprocess.run(["systemctl", "enable", _unit_name(hub_site.id)], capture_output=True)
            r = subprocess.run(["systemctl", "restart", _unit_name(hub_site.id)],
                               capture_output=True, text=True)

        ok = r.returncode == 0
        msg = (r.stderr or r.stdout).strip()
        return ok, msg or ("OK" if ok else "ошибка применения конфига")

    def hub_teardown(self, hub_site) -> None:
        subprocess.run(["systemctl", "disable", "--now", _unit_name(hub_site.id)],
                       capture_output=True)
        for p in (_unit_path(hub_site.id), _conf_path(hub_site.id)):
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["systemctl", "daemon-reload"], check=False)

    def site_config(self, hub_site, site, peer_sites: list | None = None) -> str:
        return _build_spoke_conf(hub_site, site, peer_sites)

    def status(self, hub_site) -> list[dict]:
        r = subprocess.run(["wg", "show", _iface(hub_site.id), "dump"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return []
        lines = r.stdout.strip().splitlines()
        now = time.time()
        peers = []
        for line in lines[1:]:  # первая строка — интерфейс
            parts = line.split("\t")
            if len(parts) < 8:
                continue
            pub, psk, endpoint, allowed, last_hs, rx, tx, keepalive = parts[:8]
            try:
                hs = int(last_hs)
                online = (now - hs) < 180 if hs > 0 else False
            except ValueError:
                online = False
            peers.append({
                "pubkey": pub,
                "endpoint": endpoint if endpoint != "(none)" else None,
                "last_handshake": int(last_hs) if last_hs.isdigit() else 0,
                "rx": int(rx) if rx.isdigit() else 0,
                "tx": int(tx) if tx.isdigit() else 0,
                "online": online,
            })
        return peers


# Регистрируем при импорте
register(WireGuardTransport())
