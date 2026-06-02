"""Управление WireGuard серверами и пирами."""
import os
import subprocess
import ipaddress

DATA_DIR = os.getenv("DATA_DIR", "./data")
WG_DIR = os.path.join(DATA_DIR, "wireguard")


def _iface(server_id: int) -> str:
    return f"wg{server_id}"


def _conf_path(server_id: int) -> str:
    return os.path.join(WG_DIR, f"wg{server_id}.conf")


def _unit_name(server_id: int) -> str:
    return f"click-vpn-wg-{server_id}.service"


def _unit_path(server_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(server_id)}"


def gen_keypair() -> tuple[str, str]:
    """Возвращает (private_key, public_key)."""
    priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True).stdout.strip()
    return priv, pub


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
    """Адрес сервера = первый хост сети (.1)."""
    net = ipaddress.ip_network(f"{network}/{netmask}", strict=False)
    return str(list(net.hosts())[0])


def next_client_ip(network: str, netmask: str, used: list[str]) -> str:
    """Следующий свободный IP для клиента (server занимает .1)."""
    net = ipaddress.ip_network(f"{network}/{netmask}", strict=False)
    hosts = list(net.hosts())
    server_ip = str(hosts[0])
    used_set = set(used) | {server_ip}
    for h in hosts[1:]:
        if str(h) not in used_set:
            return str(h)
    raise RuntimeError("Нет свободных IP в подсети")


def build_server_conf(server, peers: list[dict]) -> str:
    """peers: [{'public_key':..., 'address':...}]"""
    cidr = ipaddress.ip_network(f"{server.network}/{server.netmask}", strict=False)
    addr = server_address(server.network, server.netmask)
    prefix = cidr.prefixlen
    iface = _default_iface()
    snet = str(cidr)

    postup = (f"sysctl -w net.ipv4.ip_forward=1; "
              f"iptables -t nat -A POSTROUTING -s {snet} -o {iface} -j MASQUERADE; "
              f"iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT")
    postdown = (f"iptables -t nat -D POSTROUTING -s {snet} -o {iface} -j MASQUERADE; "
                f"iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT")

    lines = [
        "[Interface]",
        f"Address = {addr}/{prefix}",
        f"ListenPort = {server.port}",
        f"PrivateKey = {server.wg_private_key}",
        f"PostUp = {postup}",
        f"PostDown = {postdown}",
        "",
    ]
    for p in peers:
        lines += [
            "[Peer]",
            f"PublicKey = {p['public_key']}",
            f"AllowedIPs = {p['address']}/32",
            "",
        ]
    return "\n".join(lines) + "\n"


def write_and_sync(server, peers: list[dict]):
    """Пишет конфиг и применяет к живому интерфейсу (если поднят)."""
    os.makedirs(WG_DIR, exist_ok=True)
    conf = build_server_conf(server, peers)
    path = _conf_path(server.id)
    with open(path, "w") as f:
        f.write(conf)
    os.chmod(path, 0o600)
    # если интерфейс поднят — синхронизируем без обрыва
    if is_running(server.id):
        subprocess.run(
            ["bash", "-c", f"wg syncconf {_iface(server.id)} <(wg-quick strip {path})"],
            capture_output=True
        )


def create_unit(server_id: int):
    path = _conf_path(server_id)
    unit = f"""[Unit]
Description=Click VPN WireGuard {server_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/wg-quick up {path}
ExecStop=/usr/bin/wg-quick down {path}

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(server_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def start(server_id: int) -> tuple[bool, str]:
    create_unit(server_id)
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
    r = subprocess.run(["wg", "show", _iface(server_id)], capture_output=True)
    return r.returncode == 0


def build_client_conf(client_priv: str, client_addr: str, server_pub: str,
                      endpoint_host: str, endpoint_port: int, dns: str,
                      allowed_ips: str) -> str:
    dns_line = f"DNS = {dns}\n" if dns else ""
    return (
        "[Interface]\n"
        f"PrivateKey = {client_priv}\n"
        f"Address = {client_addr}/32\n"
        f"{dns_line}"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_pub}\n"
        f"Endpoint = {endpoint_host}:{endpoint_port}\n"
        f"AllowedIPs = {allowed_ips}\n"
        "PersistentKeepalive = 25\n"
    )


def show_peers(server_id: int) -> list[dict]:
    """Парсит `wg show <iface> dump` -> список пиров с последним handshake/трафиком."""
    r = subprocess.run(["wg", "show", _iface(server_id), "dump"], capture_output=True, text=True)
    if r.returncode != 0:
        return []
    peers = []
    lines = r.stdout.strip().splitlines()
    for line in lines[1:]:  # первая строка — интерфейс
        f = line.split("\t")
        if len(f) >= 8:
            peers.append({
                "public_key": f[0],
                "endpoint": f[3],
                "allowed_ips": f[4],
                "last_handshake": int(f[5]) if f[5].isdigit() else 0,
                "rx": int(f[6]) if f[6].isdigit() else 0,
                "tx": int(f[7]) if f[7].isdigit() else 0,
            })
    return peers
