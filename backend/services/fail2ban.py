"""Управление fail2ban из панели: статус jail, список банов, ручной бан/разбан.

Jail называется `click-vpn-openvpn` (создаётся install-fail2ban.sh).
Все вызовы безопасно деградируют, если fail2ban не установлен.
"""
import shutil
import subprocess

JAIL = "click-vpn-openvpn"


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
