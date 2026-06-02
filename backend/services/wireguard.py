"""Управление WireGuard и AmneziaWG серверами/пирами.

Поддерживаемые kind:
  wireguard          -> бинарь wg / wg-quick
  amneziawg          -> awg / awg-quick (обфускация, v2: H/S/J + I1..I5)
  amneziawg_legacy   -> awg / awg-quick (обфускация 1.5: H/S/J)
"""
import os
import json
import secrets
import subprocess
import ipaddress

DATA_DIR = os.getenv("DATA_DIR", "./data")
WG_DIR = os.path.join(DATA_DIR, "wireguard")


def is_amnezia(kind: str) -> bool:
    return kind in ("amneziawg", "amneziawg_legacy")


def _engine(kind: str) -> tuple[str, str]:
    """Возвращает (wg-бинарь, quick-бинарь)."""
    if is_amnezia(kind):
        return "awg", "awg-quick"
    return "wg", "wg-quick"


def _iface(server_id: int) -> str:
    return f"wg{server_id}" if True else ""


def _conf_path(server_id: int) -> str:
    return os.path.join(WG_DIR, f"wg{server_id}.conf")


def _unit_name(server_id: int) -> str:
    return f"click-vpn-wg-{server_id}.service"


def _unit_path(server_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(server_id)}"


def gen_keypair(kind: str = "wireguard") -> tuple[str, str]:
    wg, _ = _engine(kind)
    priv = subprocess.check_output([wg, "genkey"], text=True).strip()
    pub = subprocess.run([wg, "pubkey"], input=priv, capture_output=True, text=True).stdout.strip()
    return priv, pub


def gen_awg_params(version: str = "2") -> dict:
    """Генерирует параметры обфускации AmneziaWG.
    version: '2' (с I-пакетами) или 'legacy' (только H/S/J)."""
    # H1..H4 — различные значения > 4 (1..4 зарезервированы под типы handshake)
    hs = set()
    while len(hs) < 4:
        hs.add(secrets.randbelow(2_000_000_000) + 5)
    h1, h2, h3, h4 = sorted(hs)
    p = {
        "Jc": secrets.randbelow(8) + 4,     # 4..11 мусорных пакетов
        "Jmin": 40,
        "Jmax": 70,
        "S1": secrets.randbelow(100) + 15,  # размер junk до handshake init
        "S2": secrets.randbelow(100) + 15,  # после handshake resp
        "H1": h1, "H2": h2, "H3": h3, "H4": h4,
    }
    if version == "2":
        # сигнатурные junk-пакеты (случайный hex)
        for i in (1, 2, 3):
            blob = secrets.token_hex(secrets.randbelow(16) + 8)
            p[f"I{i}"] = f"<b 0x{blob}>"
    return p


def _awg_param_lines(params: dict | None) -> list[str]:
    if not params:
        return []
    order = ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4",
             "I1", "I2", "I3", "I4", "I5"]
    lines = []
    for k in order:
        if k in params and params[k] not in (None, ""):
            lines.append(f"{k} = {params[k]}")
    return lines


def _default_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"],
                                      stderr=subprocess.DEVNULL, text=True).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


def server_address(network: str, netmask: str) -> str:
    net = ipaddress.ip_network(f"{network}/{netmask}", strict=False)
    return str(list(net.hosts())[0])


def next_client_ip(network: str, netmask: str, used: list[str]) -> str:
    net = ipaddress.ip_network(f"{network}/{netmask}", strict=False)
    hosts = list(net.hosts())
    server_ip = str(hosts[0])
    used_set = set(used) | {server_ip}
    for h in hosts[1:]:
        if str(h) not in used_set:
            return str(h)
    raise RuntimeError("Нет свободных IP в подсети")


def _awg_params(server) -> dict | None:
    if not is_amnezia(server.kind):
        return None
    try:
        return json.loads(server.awg_params) if server.awg_params else None
    except Exception:
        return None


def build_server_conf(server, peers: list[dict]) -> str:
    cidr = ipaddress.ip_network(f"{server.network}/{server.netmask}", strict=False)
    addr = server_address(server.network, server.netmask)
    iface = _default_iface()
    snet = str(cidr)

    postup = (f"sysctl -w net.ipv4.ip_forward=1; "
              f"iptables -t nat -A POSTROUTING -s {snet} -o {iface} -j MASQUERADE; "
              f"iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT")
    postdown = (f"iptables -t nat -D POSTROUTING -s {snet} -o {iface} -j MASQUERADE; "
                f"iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT")

    lines = ["[Interface]", f"Address = {addr}/{cidr.prefixlen}",
             f"ListenPort = {server.port}", f"PrivateKey = {server.wg_private_key}"]
    lines += _awg_param_lines(_awg_params(server))   # параметры обфускации
    lines += [f"PostUp = {postup}", f"PostDown = {postdown}", ""]
    for p in peers:
        lines += ["[Peer]", f"PublicKey = {p['public_key']}",
                  f"AllowedIPs = {p['address']}/32", ""]
    return "\n".join(lines) + "\n"


def write_and_sync(server, peers: list[dict]):
    os.makedirs(WG_DIR, exist_ok=True)
    conf = build_server_conf(server, peers)
    path = _conf_path(server.id)
    with open(path, "w") as f:
        f.write(conf)
    os.chmod(path, 0o600)
    if is_running(server.id):
        wg, quick = _engine(server.kind)
        subprocess.run(["bash", "-c", f"{wg} syncconf {_iface(server.id)} <({quick} strip {path})"],
                       capture_output=True)


def create_unit(server_id: int, kind: str):
    path = _conf_path(server_id)
    _, quick = _engine(kind)
    # AmneziaWG в LXC: модуля ядра нет -> userspace amneziawg-go
    env = ""
    if is_amnezia(kind):
        env = "Environment=AWG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go\n"
    unit = f"""[Unit]
Description=Click VPN WireGuard {server_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
{env}ExecStart=/usr/bin/{quick} up {path}
ExecStop=/usr/bin/{quick} down {path}

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(server_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def start(server_id: int, kind: str = "wireguard") -> tuple[bool, str]:
    create_unit(server_id, kind)
    subprocess.run(["systemctl", "enable", _unit_name(server_id)], capture_output=True)
    r = subprocess.run(["systemctl", "restart", _unit_name(server_id)], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout)


def stop(server_id: int):
    subprocess.run(["systemctl", "disable", _unit_name(server_id)], capture_output=True)
    subprocess.run(["systemctl", "stop", _unit_name(server_id)], capture_output=True)


def remove(server_id: int):
    stop(server_id)
    for p in (_unit_path(server_id), _conf_path(server_id)):
        if os.path.exists(p):
            os.remove(p)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def is_running(server_id: int) -> bool:
    # пробуем обоими бинарями
    for wg in ("wg", "awg"):
        r = subprocess.run([wg, "show", _iface(server_id)], capture_output=True)
        if r.returncode == 0:
            return True
    return False


def build_client_conf(client_priv: str, client_addr: str, server_pub: str,
                      endpoint_host: str, endpoint_port: int, dns: str,
                      allowed_ips: str, awg_params: dict | None = None) -> str:
    dns_line = f"DNS = {dns}\n" if dns else ""
    iface = [f"PrivateKey = {client_priv}", f"Address = {client_addr}/32"]
    if dns:
        iface.append(f"DNS = {dns}")
    iface += _awg_param_lines(awg_params)
    return (
        "[Interface]\n" + "\n".join(iface) + "\n\n"
        "[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"Endpoint = {endpoint_host}:{endpoint_port}\n"
        f"AllowedIPs = {allowed_ips}\n"
        "PersistentKeepalive = 25\n"
    )
