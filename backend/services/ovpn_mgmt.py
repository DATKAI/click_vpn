"""Взаимодействие с management-интерфейсом OpenVPN (unix-сокет)."""
import os
import socket

DATA_DIR = os.getenv("DATA_DIR", "./data")


def _sock_path(server_id: int) -> str:
    return os.path.join(DATA_DIR, "openvpn", f"mgmt_{server_id}.sock")


def _send(server_id: int, command: str, timeout: float = 3.0) -> str:
    """Подключается к management-сокету, шлёт команду, возвращает ответ."""
    path = _sock_path(server_id)
    if not os.path.exists(path):
        raise FileNotFoundError("management-сокет не найден (пересоздайте сервер)")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        try:
            s.recv(4096)  # приветственный баннер
        except socket.timeout:
            pass
        s.sendall((command + "\n").encode())
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"END" in data or b"SUCCESS" in data or b"ERROR" in data:
                    break
        except socket.timeout:
            pass
        return data.decode(errors="ignore")
    finally:
        try:
            s.close()
        except Exception:
            pass


def kill_client(server_id: int, common_name: str) -> tuple[bool, str]:
    """Разрывает все активные сессии клиента по common name.
    Возвращает (успех, сообщение)."""
    try:
        resp = _send(server_id, f"kill {common_name}")
    except FileNotFoundError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Ошибка management: {e}"

    if "SUCCESS" in resp:
        return True, resp.strip().splitlines()[-1] if resp.strip() else "OK"
    if "ERROR" in resp:
        # ERROR: common name 'x' not found — значит уже не подключён
        return True, "Клиент не подключён (нечего разрывать)"
    return True, resp.strip() or "OK"


def is_available(server_id: int) -> bool:
    return os.path.exists(_sock_path(server_id))
