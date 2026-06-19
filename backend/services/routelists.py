"""Компилятор списков маршрутов для селективной маршрутизации (split tunnel).

Источники профиля → разрешение в IP/CIDR → агрегация (collapse) → файл в
DATA_DIR/routelists/<profile_id>.txt. Скомпилированный набор подставляется в
AllowedIPs клиентского конфига.

R0/R1: kind = cidr | domain | url_list. provider/asn — заглушки (R2).
"""
import ipaddress
import os
import socket
import urllib.request
from datetime import datetime

DATA_DIR = os.getenv("DATA_DIR", "./data")
LIST_DIR = os.path.join(DATA_DIR, "routelists")

# Встроенные провайдеры (R2): URL опубликованных диапазонов. Пока задел.
PROVIDERS = {
    "cloudflare": ["https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"],
    "google":     ["https://www.gstatic.com/ipranges/goog.json"],
}


def list_path(profile_id: int) -> str:
    return os.path.join(LIST_DIR, f"{profile_id}.txt")


def _parse_cidrs(text: str) -> list:
    """Вытаскивает все CIDR/IP из произвольного текста (по строкам)."""
    out = []
    for line in text.splitlines():
        tok = line.strip().split()[0] if line.strip() else ""
        if not tok or tok.startswith("#"):
            continue
        try:
            out.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            try:
                out.append(ipaddress.ip_network(tok + "/32", strict=False))
            except ValueError:
                pass
    return out


def _resolve_domain(domain: str) -> list:
    nets = []
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(domain, None):
            ip = sockaddr[0]
            try:
                pref = 32 if ":" not in ip else 128
                nets.append(ipaddress.ip_network(f"{ip}/{pref}", strict=False))
            except ValueError:
                pass
    except Exception:
        pass
    return nets


def _fetch_url(url: str) -> list:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "click-vpn"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read().decode("utf-8", "ignore")
        if data.lstrip().startswith("{"):
            # JSON провайдера (Google goog.json: {prefixes:[{ipv4Prefix|ipv6Prefix}]})
            import json
            j = json.loads(data)
            nets = []
            for p in j.get("prefixes", []):
                cidr = p.get("ipv4Prefix") or p.get("ipv6Prefix")
                if cidr:
                    try:
                        nets.append(ipaddress.ip_network(cidr, strict=False))
                    except ValueError:
                        pass
            return nets
        return _parse_cidrs(data)
    except Exception:
        return []


def _collect(source) -> list:
    k, v = source.kind, (source.value or "").strip()
    if k == "cidr":
        return _parse_cidrs(v)
    if k == "domain":
        return _resolve_domain(v)
    if k == "url_list":
        return _fetch_url(v)
    if k == "provider":
        nets = []
        for url in PROVIDERS.get(v.lower(), []):
            nets += _fetch_url(url)
        return nets
    if k == "asn":
        return _fetch_asn(v)
    return []


def _fetch_asn(asn: str) -> list:
    """Префиксы автономной системы через RIPEstat (без ключа)."""
    asn = asn.upper().lstrip("AS")
    url = f"https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "click-vpn"})
        with urllib.request.urlopen(req, timeout=25) as r:
            import json
            j = json.loads(r.read().decode("utf-8", "ignore"))
        nets = []
        for p in j.get("data", {}).get("prefixes", []):
            try:
                nets.append(ipaddress.ip_network(p["prefix"], strict=False))
            except (ValueError, KeyError):
                pass
        return nets
    except Exception:
        return []


def compile_profile(db, profile) -> int:
    """Собирает все источники профиля в агрегированный набор CIDR, пишет файл.
    Возвращает число префиксов."""
    v4, v6 = [], []
    for src in profile.sources:
        if not src.enabled:
            continue
        for net in _collect(src):
            (v6 if net.version == 6 else v4).append(net)

    v4 = list(ipaddress.collapse_addresses(v4))
    v6 = list(ipaddress.collapse_addresses(v6))
    cidrs = [str(n) for n in v4] + [str(n) for n in v6]

    os.makedirs(LIST_DIR, exist_ok=True)
    with open(list_path(profile.id), "w") as f:
        f.write("\n".join(cidrs) + ("\n" if cidrs else ""))

    profile.prefix_count = len(cidrs)
    profile.compiled_at = datetime.utcnow()
    db.commit()
    return len(cidrs)


def load_cidrs(profile_id: int) -> list[str]:
    try:
        with open(list_path(profile_id)) as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


def allowed_ips_for(profile, base_routes: list[str]) -> list[str]:
    """Вычисляет AllowedIPs для клиента по профилю.

    base_routes — текущие маршруты сервера (push_routes/сеть), всегда включаются,
    чтобы доступ к самому VPN/его сети сохранялся.
    """
    cidrs = load_cidrs(profile.id)
    if profile.mode == "full":
        return ["0.0.0.0/0", "::/0"]
    if profile.mode == "exclude":
        # всё кроме указанных подсетей (вычитание из 0.0.0.0/0)
        excl4 = [ipaddress.ip_network(c) for c in cidrs if ":" not in c]
        full = [ipaddress.ip_network("0.0.0.0/0")]
        for e in excl4:
            full = _exclude(full, e)
        res = [str(n) for n in full]
    else:  # selective
        res = list(cidrs)
    # служебные маршруты сервера + DNS через туннель
    for r in base_routes:
        if r not in res:
            res.append(r)
    if profile.dns_through_tunnel and profile.dns_server:
        d = profile.dns_server.strip() + "/32"
        if d not in res:
            res.append(d)
    return res


def _exclude(nets: list, remove) -> list:
    out = []
    for n in nets:
        if n.overlaps(remove):
            try:
                out += list(n.address_exclude(remove))
            except ValueError:
                out.append(n)
        else:
            out.append(n)
    return out
