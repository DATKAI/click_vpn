"""Управление OpenVPN серверами через systemd."""
import os
import subprocess
import ipaddress


def _unit_name(server_id: int) -> str:
    return f"click-vpn-server-{server_id}.service"


def _unit_path(server_id: int) -> str:
    return f"/etc/systemd/system/{_unit_name(server_id)}"


def _get_default_iface() -> str:
    """Определяет основной сетевой интерфейс."""
    try:
        out = subprocess.check_output(
            ["ip", "route", "get", "8.8.8.8"],
            stderr=subprocess.DEVNULL, text=True
        )
        for part in out.split():
            if part not in ("8.8.8.8", "via", "dev", "src", "uid") and "." not in part and part.isalpha() or "eth" in part or "ens" in part or "enp" in part:
                return part
    except Exception:
        pass
    # Fallback
    try:
        out = subprocess.check_output(["ip", "-o", "link", "show", "up"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            name = line.split(":")[1].strip().split("@")[0]
            if name != "lo" and not name.startswith("tun"):
                return name
    except Exception:
        pass
    return "eth0"


def _nat_rules(action: str, network: str, netmask: str, iface: str):
    """Добавляет или удаляет NAT правило iptables."""
    try:
        net = ipaddress.ip_network(f"{network}/{netmask}", strict=False)
        cidr = str(net)
        flag = "-A" if action == "add" else "-D"
        subprocess.run(
            ["iptables", flag, "POSTROUTING", "-t", "nat",
             "-s", cidr, "-o", iface, "-j", "MASQUERADE"],
            check=False, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def create_systemd_unit(server_id: int, config_path: str):
    """Создаёт systemd unit файл для OpenVPN сервера."""
    unit = f"""[Unit]
Description=Click VPN Server {server_id}
After=network.target click-vpn.service
PartOf=click-vpn.service

[Service]
Type=forking
PIDFile=/var/lib/click-vpn/openvpn/server_{server_id}.pid
ExecStart=/usr/sbin/openvpn --config {config_path} --writepid /var/lib/click-vpn/openvpn/server_{server_id}.pid --daemon click-vpn-{server_id}
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
    """Запускает OpenVPN сервер через systemd."""
    try:
        # Создаём unit если нет
        if not os.path.exists(_unit_path(server_id)):
            create_systemd_unit(server_id, config_path)

        result = subprocess.run(
            ["systemctl", "start", _unit_name(server_id)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return False

        # NAT
        iface = _get_default_iface()
        _nat_rules("add", network, netmask, iface)

        # ip_forward
        try:
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write("1")
        except Exception:
            pass

        return True
    except Exception:
        return False


def stop_server(server_id: int, data_dir: str,
                network: str = "10.8.0.0", netmask: str = "255.255.255.0") -> bool:
    """Останавливает OpenVPN сервер."""
    try:
        subprocess.run(
            ["systemctl", "stop", _unit_name(server_id)],
            capture_output=True
        )
        iface = _get_default_iface()
        _nat_rules("remove", network, netmask, iface)
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
    """Парсит status log OpenVPN для получения подключённых клиентов."""
    clients = []
    if not os.path.exists(status_log_path):
        return clients
    try:
        with open(status_log_path) as f:
            lines = f.readlines()
    except OSError:
        return clients

    in_client_list = False
    for line in lines:
        line = line.strip()
        if line.startswith("Common Name,Real Address"):
            in_client_list = True
            continue
        if line.startswith("ROUTING TABLE") or line.startswith("GLOBAL STATS"):
            in_client_list = False
            continue
        if in_client_list and line:
            parts = line.split(",")
            if len(parts) >= 5:
                clients.append({
                    "common_name": parts[0],
                    "real_address": parts[1],
                    "virtual_address": parts[2],
                    "bytes_received": int(parts[3]) if parts[3].isdigit() else 0,
                    "bytes_sent": int(parts[4]) if parts[4].isdigit() else 0,
                    "connected_since": parts[5] if len(parts) > 5 else "",
                })
    return clients
