"""Опциональные компоненты сервера и их установка из панели.

Каждый компонент = install-скрипт в корне проекта + проверка наличия.
Установка запускается отдельным процессом, вывод пишется в лог.
"""
import os
import shutil
import subprocess

INSTALL_DIR = os.getenv("INSTALL_DIR",
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_DIR = os.getenv("DATA_DIR", "/var/lib/click-vpn")


def _which(b: str) -> bool:
    return shutil.which(b) is not None


def _unit_exists(unit: str) -> bool:
    out = subprocess.run(["systemctl", "list-unit-files"], capture_output=True, text=True).stdout
    return unit in out


COMPONENTS = [
    {"id": "amneziawg", "name": "AmneziaWG",
     "desc": "Обфусцированный WireGuard (H/S/J + I-пакеты) — устойчивость к DPI.",
     "script": "install-amneziawg.sh", "check": lambda: _which("awg")},
    {"id": "ikev2", "name": "IKEv2 / strongSwan",
     "desc": "IPsec/IKEv2 сервер и транспорт site-to-site (swanctl).",
     "script": "install-ikev2.sh", "check": lambda: _which("swanctl")},
    {"id": "fail2ban", "name": "fail2ban",
     "desc": "Автобан сканеров по неудачным попыткам подключения.",
     "script": "install-fail2ban.sh", "check": lambda: _which("fail2ban-client")},
    {"id": "client_installer", "name": "Windows-установщик (NSIS)",
     "desc": "Сборка .exe-установщика клиента (NSIS + OpenVPN MSI).",
     "script": "install-client-installer.sh", "check": lambda: _which("makensis")},
    {"id": "share", "name": "Сервис раздачи ссылок",
     "desc": "Изолированный сервис временных ссылок на скачивание конфигов.",
     "script": "install-share-service.sh",
     "check": lambda: _unit_exists("click-vpn-share.service")},
]


def _by_id(cid: str) -> dict | None:
    return next((c for c in COMPONENTS if c["id"] == cid), None)


def _log_path(cid: str) -> str:
    return os.path.join(DATA_DIR, f"install-{cid}.log")


def _running_path(cid: str) -> str:
    return os.path.join(DATA_DIR, f"install-{cid}.running")


def status() -> list[dict]:
    out = []
    for c in COMPONENTS:
        try:
            installed = bool(c["check"]())
        except Exception:
            installed = False
        out.append({"id": c["id"], "name": c["name"], "desc": c["desc"],
                    "installed": installed,
                    "running": os.path.exists(_running_path(c["id"]))})
    return out


def install(cid: str) -> dict:
    c = _by_id(cid)
    if not c:
        raise ValueError("Неизвестный компонент")
    script = os.path.join(INSTALL_DIR, c["script"])
    if not os.path.exists(script):
        raise FileNotFoundError(f"Скрипт не найден: {script}")
    if os.path.exists(_running_path(cid)):
        return {"started": False, "reason": "уже выполняется"}

    open(_running_path(cid), "w").close()
    # обёртка: пишет лог и убирает флаг running по завершении
    wrapper = (f"bash {script} > {_log_path(cid)} 2>&1; "
               f"rm -f {_running_path(cid)}")
    # systemd-run — чистый root-юнит вне песочницы сервиса (иначе apt не может
    # сбросить привилегии до _apt: setgroups/seteuid Operation not permitted).
    if shutil.which("systemd-run"):
        r = subprocess.run(["systemd-run", "--collect", "--quiet",
                            f"--unit=clickvpn-install-{cid}", "bash", "-c", wrapper],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return {"started": True}
    subprocess.Popen(["bash", "-c", wrapper], cwd=INSTALL_DIR, start_new_session=True)
    return {"started": True}


def log(cid: str, tail: int = 200) -> str:
    try:
        with open(_log_path(cid)) as f:
            lines = f.readlines()
        return "".join(lines[-tail:])
    except Exception:
        return ""
