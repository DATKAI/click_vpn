"""Управление fail2ban из панели: статус jail, список банов, ручной бан/разбан.

Jail называется `click-vpn-openvpn` (создаётся install-fail2ban.sh).
Все вызовы безопасно деградируют, если fail2ban не установлен.
"""
import os
import re
import shutil
import subprocess

JAIL = "click-vpn-openvpn"
JAIL_FILE = "/etc/fail2ban/jail.d/click-vpn.conf"

DEFAULTS = {"maxretry": 5, "findtime": 600, "bantime": 3600, "ignoreip": ""}


def get_config() -> dict:
    """Читает параметры jail из конфиг-файла."""
    cfg = dict(DEFAULTS)
    cfg["jail_file"] = JAIL_FILE
    if not os.path.exists(JAIL_FILE):
        return cfg
    try:
        with open(JAIL_FILE) as f:
            text = f.read()
    except OSError:
        return cfg
    for key in ("maxretry", "findtime", "bantime"):
        m = re.search(rf"^\s*{key}\s*=\s*(-?\d+)", text, re.MULTILINE)
        if m:
            cfg[key] = int(m.group(1))
    m = re.search(r"^\s*ignoreip\s*=\s*(.+)$", text, re.MULTILINE)
    if m:
        # убираем дефолтные локальные адреса из показа
        ips = m.group(1).strip().split()
        ips = [ip for ip in ips if ip not in ("127.0.0.1/8", "::1")]
        cfg["ignoreip"] = " ".join(ips)
    return cfg


def set_config(maxretry: int, findtime: int, bantime: int, ignoreip: str) -> tuple[bool, str]:
    """Перезаписывает jail-файл и перезагружает fail2ban."""
    if not is_installed():
        return False, "fail2ban не установлен"
    # валидация
    maxretry = max(1, min(int(maxretry), 1000))
    findtime = max(10, min(int(findtime), 86400 * 7))
    bantime = int(bantime)  # -1 = навсегда
    # белый список: локальные + пользовательские (чистим спецсимволы)
    user_ips = " ".join(re.findall(r"[0-9a-fA-F:\.\/]+", ignoreip or ""))
    ignore = ("127.0.0.1/8 ::1 " + user_ips).strip()

    conf = f"""[click-vpn-openvpn]
enabled  = true
backend  = systemd
filter   = click-vpn-openvpn
maxretry = {maxretry}
findtime = {findtime}
bantime  = {bantime}
ignoreip = {ignore}
action   = iptables-allports[name=click-vpn]
"""
    try:
        os.makedirs(os.path.dirname(JAIL_FILE), exist_ok=True)
        with open(JAIL_FILE, "w") as f:
            f.write(conf)
    except OSError as e:
        return False, f"не удалось записать конфиг: {e}"

    r = subprocess.run(["fail2ban-client", "reload", JAIL], capture_output=True, text=True)
    if r.returncode != 0:
        # полный перезапук как фолбэк
        subprocess.run(["fail2ban-client", "reload"], capture_output=True)
    return True, "Настройки применены"


def is_installed() -> bool:
    """fail2ban-client доступен в PATH?"""
    return shutil.which("fail2ban-client") is not None


def _run(args: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["fail2ban-client"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)


def jail_exists() -> bool:
    ok, out = _run(["status"])
    if not ok:
        return False
    # "Jail list:  click-vpn-openvpn, sshd"
    for line in out.splitlines():
        if "Jail list" in line:
            jails = [j.strip() for j in line.split(":", 1)[1].split(",")]
            return JAIL in jails
    return False


def status() -> dict:
    """Возвращает сводку jail + список забаненных IP."""
    result = {
        "installed": is_installed(),
        "jail_active": False,
        "banned_ips": [],
        "currently_banned": 0,
        "total_banned": 0,
        "currently_failed": 0,
        "total_failed": 0,
    }
    if not result["installed"]:
        return result

    ok, out = _run(["status", JAIL])
    if not ok:
        return result
    result["jail_active"] = True

    for raw in out.splitlines():
        line = raw.strip().lstrip("|`- ").strip()
        if line.startswith("Currently failed:"):
            result["currently_failed"] = _int(line)
        elif line.startswith("Total failed:"):
            result["total_failed"] = _int(line)
        elif line.startswith("Currently banned:"):
            result["currently_banned"] = _int(line)
        elif line.startswith("Total banned:"):
            result["total_banned"] = _int(line)
        elif line.startswith("Banned IP list:"):
            ips = line.split(":", 1)[1].strip()
            result["banned_ips"] = [ip for ip in ips.split() if ip]
    return result


def _int(line: str) -> int:
    try:
        return int(line.split(":", 1)[1].strip())
    except Exception:
        return 0


def ban(ip: str) -> tuple[bool, str]:
    if not is_installed():
        return False, "fail2ban не установлен (bash install-fail2ban.sh)"
    ok, out = _run(["set", JAIL, "banip", ip])
    if not ok:
        return False, out.strip() or "не удалось забанить"
    return True, f"IP {ip} забанен"


def unban(ip: str) -> tuple[bool, str]:
    if not is_installed():
        return False, "fail2ban не установлен"
    ok, out = _run(["set", JAIL, "unbanip", ip])
    if not ok:
        return False, out.strip() or "не удалось разбанить"
    return True, f"IP {ip} разбанен"
