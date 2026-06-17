"""Ограничение скорости (traffic shaping) через tc HTB.

Ограничивает download (трафик от сервера к клиенту) по VPN-IP клиента на
tun/wg-интерфейсе сервера. Upload не ограничивается (нужен ifb — отдельно).

Идемпотентная синхронизация: sync(iface, {ip: mbps}) приводит шейпинг
интерфейса к желаемому состоянию (добавляет/меняет/снимает классы).
"""
import subprocess

# Применённое состояние: {iface: {ip: mbps}}
_applied: dict = {}


def _run(args) -> bool:
    try:
        r = subprocess.run(["tc"] + args, capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _has_tc() -> bool:
    import shutil
    return shutil.which("tc") is not None


def _classid(ip: str) -> int:
    """Уникальный minor classid из последнего октета IP (пул /24)."""
    try:
        last = int(ip.split(".")[-1])
    except Exception:
        last = 1
    return 100 + (last % 800)   # 100..899 (1:1 и 1:999 зарезервированы)


def _ensure_root(iface: str):
    """HTB root qdisc + дефолтный безлимитный класс."""
    # проверяем, есть ли уже наш htb root
    try:
        out = subprocess.run(["tc", "qdisc", "show", "dev", iface],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    if "htb 1:" not in out:
        _run(["qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "999"])
        _run(["class", "add", "dev", iface, "parent", "1:", "classid", "1:999",
              "htb", "rate", "1000mbit"])
    return True


def _set_limit(iface: str, ip: str, mbps: int):
    cid = _classid(ip)
    rate = f"{mbps}mbit"
    _run(["class", "replace", "dev", iface, "parent", "1:", "classid", f"1:{cid}",
          "htb", "rate", rate, "ceil", rate])
    # u32 filter по dst-IP → класс; prio уникален по cid
    _run(["filter", "replace", "dev", iface, "protocol", "ip", "parent", "1:",
          "prio", str(cid), "u32", "match", "ip", "dst", f"{ip}/32", "flowid", f"1:{cid}"])


def _clear_limit(iface: str, ip: str):
    cid = _classid(ip)
    _run(["filter", "del", "dev", iface, "protocol", "ip", "parent", "1:", "prio", str(cid)])
    _run(["class", "del", "dev", iface, "parent", "1:", "classid", f"1:{cid}"])


def sync(iface: str, limits: dict):
    """Приводит шейпинг iface к {ip: mbps}. limits может быть пустым."""
    if not _has_tc() or not iface:
        return
    prev = _applied.get(iface, {})
    if limits and not _ensure_root(iface):
        return

    # добавить/обновить
    for ip, mbps in limits.items():
        if mbps and prev.get(ip) != mbps:
            _set_limit(iface, ip, mbps)
    # снять исчезнувшие
    for ip in list(prev.keys()):
        if ip not in limits:
            _clear_limit(iface, ip)

    _applied[iface] = dict(limits)


def clear_all(iface: str):
    """Полностью снять шейпинг с интерфейса."""
    if not _has_tc() or not iface:
        return
    _run(["qdisc", "del", "dev", iface, "root"])
    _applied.pop(iface, None)
