"""Системные метрики сервера без внешних зависимостей (только /proc + shutil).
Рассчитан на Linux (Debian). На других ОС поля деградируют в None/0.
"""
import os
import shutil
import time

# CPU: храним предыдущий замер для вычисления % между вызовами
_prev_cpu = {"total": 0, "idle": 0, "ts": 0.0}


def _cpu_percent() -> float | None:
    """Загрузка CPU в % (по дельте /proc/stat между вызовами)."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        dt = total - _prev_cpu["total"]
        di = idle - _prev_cpu["idle"]
        _prev_cpu["total"], _prev_cpu["idle"] = total, idle
        if dt <= 0:
            return None
        return round(100.0 * (1.0 - di / dt), 1)
    except Exception:
        return None


def _mem() -> dict:
    """RAM из /proc/meminfo (в байтах)."""
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) * 1024  # kB → байт
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        return {
            "total": total, "used": used, "available": avail,
            "percent": round(100.0 * used / total, 1) if total else 0,
        }
    except Exception:
        return {"total": 0, "used": 0, "available": 0, "percent": 0}


def _disk(path: str = "/") -> dict:
    try:
        u = shutil.disk_usage(path)
        return {
            "total": u.total, "used": u.used, "free": u.free,
            "percent": round(100.0 * u.used / u.total, 1) if u.total else 0,
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0}


def _uptime() -> int | None:
    """Аптайм сервера в секундах."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.readline().split()[0]))
    except Exception:
        return None


def _loadavg() -> list[float] | None:
    try:
        return list(os.getloadavg())
    except Exception:
        return None


def _cpu_count() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1


def collect(db_path: str | None = None, data_dir: str | None = None) -> dict:
    """Собирает снимок системных метрик."""
    out = {
        "cpu_percent": _cpu_percent(),
        "cpu_count": _cpu_count(),
        "loadavg": _loadavg(),
        "mem": _mem(),
        "disk": _disk("/"),
        "uptime_sec": _uptime(),
        "db_size": 0,
        "data_dir_size": 0,
    }
    if db_path and os.path.exists(db_path):
        try:
            out["db_size"] = os.path.getsize(db_path)
        except Exception:
            pass
    if data_dir and os.path.isdir(data_dir):
        try:
            total = 0
            for root, _dirs, files in os.walk(data_dir):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        pass
            out["data_dir_size"] = total
        except Exception:
            pass
    return out
