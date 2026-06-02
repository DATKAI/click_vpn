"""Управление OpenVPN серверами через systemd."""
import os
import subprocess
import ipaddress


def _unit_name(server_id: int) -> str:
    return f"click-vpn-server-{server_id}.service"


def _unit_path(server_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(server_id)}"


def _get_default_iface() -> str:
    """Определяет основной сетевой интерфейс (токен после 'dev')."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", "8.8.8.8"],
            stderr=subprocess.DEVNULL, text=True
        ).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    # Fallback: дефолтный маршрут
    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"],
            stderr=subprocess.DEVNULL, text=True
        ).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    # Fallback: первый не-lo/не-tun интерфейс
    try:
        out = subprocess.check_output(["ip", "-o", "link", "show", "up"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            name = line.split(":")[1].strip().split("@")[0]
            if name != "lo" and not name.startswith("tun"):
                return name
    except Exception:
        pass
    return "eth0"


def _cidr(network: str, netmask: str) -> str:
    try:
        return str(ipaddress.ip_network(f"{network}/{netmask}", strict=False))
    except Exception:
        return f"{network}/24"


def create_systemd_unit(server_id: int, config_path: str,
                        network: str = "10.8.0.0", netmask: str = "255.255.255.0"):
    """Создаёт systemd unit с настройкой NAT/forward (переживает ребут)."""
    cidr = _cidr(network, netmask)
    iface = _get_default_iface()
    pid = f"/var/lib/click-vpn/openvpn/server_{server_id}.pid"

    # Идемпотентные правила (-C проверка перед -A), удаление в ExecStopPost
    add_nat = (f"iptables -t nat -C POSTROUTING -s {cidr} -o {iface} -j MASQUERADE 2>/dev/null || "
               f"iptables -t nat -A POSTROUTING -s {cidr} -o {iface} -j MASQUERADE")
    add_fwd_s = (f"iptables -C FORWARD -s {cidr} -j ACCEPT 2>/dev/null || "
                 f"iptables -A FORWARD -s {cidr} -j ACCEPT")
    add_fwd_d = (f"iptables -C FORWARD -d {cidr} -j ACCEPT 2>/dev/null || "
                 f"iptables -A FORWARD -d {cidr} -j ACCEPT")
    del_nat = f"iptables -t nat -D POSTROUTING -s {cidr} -o {iface} -j MASQUERADE 2>/dev/null || true"
    del_fwd_s = f"iptables -D FORWARD -s {cidr} -j ACCEPT 2>/dev/null || true"
    del_fwd_d = f"iptables -D FORWARD -d {cidr} -j ACCEPT 2>/dev/null || true"

    unit = f"""[Unit]
Description=Click VPN Server {server_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
PIDFile={pid}
ExecStartPre=/sbin/sysctl -w net.ipv4.ip_forward=1
ExecStart=/usr/sbin/openvpn --config {config_path} --writepid {pid} --daemon click-vpn-{server_id}
ExecStartPost=/bin/sh -c '{add_nat}'
ExecStartPost=/bin/sh -c '{add_fwd_s}'
ExecStartPost=/bin/sh -c '{add_fwd_d}'
ExecStopPost=/bin/sh -c '{del_nat}'
ExecStopPost=/bin/sh -c '{del_fwd_s}'
ExecStopPost=/bin/sh -c '{del_fwd_d}'
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    with open(_unit_path(server_id), "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def start_server(server_id: int, config_path: str, data_dir: str,
                 network: str = "10.8.0.0", netmask: str = "255.255.255.0") -> bool:
    """Запускает OpenVPN сервер через systemd (NAT/forward — в юните)."""
    try:
        # Всегда пересоздаём unit (вдруг сменился интерфейс/сеть)
        create_systemd_unit(server_id, config_path, network, netmask)
        # включаем enable чтобы поднимался при ребуте
        subprocess.run(["systemctl", "enable", _unit_name(server_id)],
                       capture_output=True)
        result = subprocess.run(
            ["systemctl", "restart", _unit_name(server_id)],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except Exception:
        return False


def stop_server(server_id: int, data_dir: str,
                network: str = "10.8.0.0", netmask: str = "255.255.255.0") -> bool:
    """Останавливает OpenVPN сервер (ExecStopPost снимет правила)."""
    try:
        subprocess.run(["systemctl", "disable", _unit_name(server_id)], capture_output=True)
        subprocess.run(["systemctl", "stop", _unit_name(server_id)], capture_output=True)
        return True
    except Exception:
        return False


def is_running(server_id: int, data_dir: str) -> bool:
    """Проверяет запущен ли сервер."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", _unit_name(server_id)],
            capture_output=True
        )
        return result.returncode == 0
    except Exception:
        return False


def remove_unit(server_id: int, data_dir: str,
                network: str = "10.8.0.0", netmask: str = "255.255.255.0"):
    """Удаляет systemd unit при удалении сервера."""
    stop_server(server_id, data_dir, network, netmask)
    unit = _unit_path(server_id)
    if os.path.exists(unit):
        os.remove(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=False)


def parse_status(status_log_path: str) -> list[dict]:
    """Парсит status log OpenVPN (формат v1).

    CLIENT LIST:  Common Name, Real Address, Bytes Received, Bytes Sent, Connected Since
    ROUTING TABLE: Virtual Address, Common Name, Real Address, Last Ref
    """
    clients = {}        # common_name -> dict
    if not os.path.exists(status_log_path):
        return []
    try:
        with open(status_log_path) as f:
            lines = f.readlines()
    except OSError:
        return []

    section = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Common Name,Real Address"):
            section = "clients"
            continue
        if line.startswith("Virtual Address,"):
            section = "routing"
            continue
        if line.startswith("ROUTING TABLE"):
            section = None
            continue
        if line.startswith("GLOBAL STATS") or line.startswith("Updated,") or line.startswith("END"):
            section = None
            continue

        parts = line.split(",")

        if section == "clients" and len(parts) >= 5:
            cn = parts[0]
            clients[cn] = {
                "common_name": cn,
                "real_address": parts[1],
                "virtual_address": "",
                "bytes_received": int(parts[2]) if parts[2].isdigit() else 0,
                "bytes_sent": int(parts[3]) if parts[3].isdigit() else 0,
                "connected_since": parts[4],
            }
        elif section == "routing" and len(parts) >= 2:
            vaddr, cn = parts[0], parts[1]
            if cn in clients and not clients[cn]["virtual_address"]:
                clients[cn]["virtual_address"] = vaddr

    return list(clients.values())
