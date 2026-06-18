"""Управление версиями приложения: просмотр версий, обновление, откат.

Деплой — git. Версии = git-теги (релизы) + коммиты. Переключение выполняет
update.sh ОТДЕЛЬНЫМ (detached) процессом: сервис при этом перезапускается, а
апдейтер переживает рестарт и при неудаче откатывается.
"""
import json
import os
import subprocess

# repo root = .../click-vpn (backend/services/updater.py → ../../..)
REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_DIR = os.getenv("INSTALL_DIR", REPO_DIR)
DATA_DIR = os.getenv("DATA_DIR", "/var/lib/click-vpn")
UPDATE_SH = os.path.join(INSTALL_DIR, "update.sh")
STATUS_FILE = os.path.join(DATA_DIR, "update-status.json")


def _git(*args, timeout=30) -> str:
    r = subprocess.run(["git", "-C", INSTALL_DIR, *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def current() -> dict:
    short = _git("rev-parse", "--short", "HEAD")
    full = _git("rev-parse", "HEAD")
    subject = _git("show", "-s", "--format=%s", "HEAD")
    date = _git("show", "-s", "--format=%cI", "HEAD")
    tags = _git("tag", "--points-at", "HEAD")
    return {
        "commit": full, "short": short, "subject": subject, "date": date,
        "tag": tags.splitlines()[0] if tags else None,
        "on_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
    }


def fetch() -> None:
    subprocess.run(["git", "-C", INSTALL_DIR, "fetch", "origin", "--tags", "-q"],
                   capture_output=True, timeout=60)


def list_versions(limit: int = 30) -> dict:
    """Релизные теги + последние коммиты origin/master, с пометкой текущего."""
    head = _git("rev-parse", "HEAD")
    versions = []

    # теги (релизы), новые сверху
    tags = _git("for-each-ref", "refs/tags", "--sort=-creatordate",
                "--format=%(refname:short)%09%(creatordate:short)%09%(*objectname)%09%(objectname)%09%(subject)")
    for line in tags.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, date, deref, objname, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        commit = deref or objname   # для аннотированных тегов *objectname = коммит
        versions.append({"type": "tag", "ref": name, "date": date,
                         "subject": subject, "commit": commit,
                         "current": commit == head})

    # последние коммиты ветки
    log = _git("log", "origin/master", f"--max-count={limit}",
               "--format=%H%09%h%09%cs%09%s")
    for line in log.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        full, short, date, subject = parts
        versions.append({"type": "commit", "ref": full, "short": short,
                         "date": date, "subject": subject, "commit": full,
                         "current": full == head})

    latest = _git("rev-parse", "origin/master")
    return {"versions": versions, "head": head,
            "update_available": latest != head, "latest": latest}


def changelog(ref: str, limit: int = 50) -> list[str]:
    """Список коммитов между текущей версией и выбранной (в обе стороны)."""
    safe = _safe_ref(ref)
    out = _git("log", "--format=%h %cs %s", f"HEAD...{safe}", f"--max-count={limit}")
    return [l for l in out.splitlines() if l.strip()]


def _safe_ref(ref: str) -> str:
    """Проверяет, что ref существует и валиден (защита от инъекций)."""
    if ref in ("latest", "origin/master"):
        return "origin/master"
    if not all(c.isalnum() or c in "._/-" for c in ref):
        raise ValueError("Некорректная ссылка версии")
    if subprocess.run(["git", "-C", INSTALL_DIR, "rev-parse", "--verify", "-q", ref + "^{commit}"],
                      capture_output=True).returncode != 0:
        raise ValueError(f"Версия не найдена: {ref}")
    return ref


def apply(ref: str) -> dict:
    """Запускает переключение версии отдельным процессом (переживает рестарт)."""
    if ref in ("latest", "origin/master"):
        args = [UPDATE_SH, "--latest"]
    else:
        _safe_ref(ref)
        args = [UPDATE_SH, "--ref", ref]
    log = open(os.path.join(DATA_DIR, "update.log"), "ab", buffering=0)
    # detached: своя сессия, чтобы пережить рестарт click-vpn
    subprocess.Popen(["bash", *args], cwd=INSTALL_DIR,
                     stdout=log, stderr=log, start_new_session=True)
    return {"started": True, "ref": ref}


def status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"state": "idle"}
