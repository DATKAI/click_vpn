"""Управление процессами OpenVPN через subprocess."""
import os
import subprocess
import signal
from pathlib import Path

# pid_file → pid
_processes: dict[str, subprocess.Popen] = {}


def _pid_file(server_id: int, data_dir: str) -> str:
    return os.path.join(data_dir, "openvpn", f"server_{server_id}.pid")


def start_server(server_id: int, config_path: str, data_dir: str) -> bool:
    key = str(server_id)
    if key in _processes and _processes[key].poll() is None:
        return True  # уже запущен

    pid_path = _pid_file(server_id, data_dir)
    try:
        proc = subprocess.Popen(
            [
                "openvpn",
                "--config", config_path,
                "--writepid", pid_path,
                "--daemon",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _processes[key] = proc
        return True
    except FileNotFoundError:
        # openvpn не установлен
        return False
    except Exception:
        return False


def stop_server(server_id: int, data_dir: str) -> bool:
    key = str(server_id)
    pid_path = _pid_file(server_id, data_dir)

    # Попытка через pid файл (если демон уже запущен)
    if os.path.exists(pid_path):
        try:
            with open(pid_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            os.remove(pid_path)
            _processes.pop(key, None)
            return True
        except (ProcessLookupError, ValueError):
            pass

    proc = _processes.get(key)
    if proc and proc.poll() is None:
        proc.terminate()
        _processes.pop(key)
        return True

    return False


def is_running(server_id: int, data_dir: str) -> bool:
    key = str(server_id)
    pid_path = _pid_file(server_id, data_dir)

    if os.path.exists(pid_path):
        try:
            with open(pid_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # проверка существования процесса
            return True
        except (ProcessLookupError, ValueError, OSError):
            return False

    proc = _processes.get(key)
    return proc is not None and proc.poll() is None


def parse_status(status_log_path: str) -> list[dict]:
    """Парсит status log файл OpenVPN для получения подключённых клиентов."""
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
