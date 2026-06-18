"""Общие сетевые операции site-to-site, не зависящие от транспорта.

Матрица доступа (FORWARD-цепочка на хабе) применяется ОДИН РАЗ на хаб поверх
всех его спиц, независимо от их транспорта (WireGuard/IPsec/...). Поэтому она
живёт здесь, а не внутри конкретного транспорта.
"""
import subprocess


def default_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"],
                                      stderr=subprocess.DEVNULL, text=True).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


def fwd_chain(hub_id: int) -> str:
    # имя цепочки ≤ 28 символов (лимит iptables)
    return f"CLICKVPN_S2S_{hub_id}"


def apply_forward_matrix(hub_site, spoke_sites: list, is_allowed=None) -> None:
    """Enforcement матрицы доступа на хабе через отдельную цепочку FORWARD.

    Даже если у площадки есть маршрут, хаб дропает межсетевой трафик между
    парами, которым доступ запрещён (defense-in-depth). Цепочка пересобирается
    целиком. Применять с ПОЛНЫМ списком спиц хаба (всех транспортов).
    """
    chain = fwd_chain(hub_site.id)
    sites = [hub_site] + list(spoke_sites)

    subprocess.run(["iptables", "-N", chain], capture_output=True)
    subprocess.run(["iptables", "-F", chain], capture_output=True)

    # установленные соединения пропускаем (иначе рвём обратный трафик разрешённых)
    subprocess.run(["iptables", "-A", chain, "-m", "conntrack",
                    "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"], capture_output=True)

    if is_allowed is not None:
        for a in sites:
            for b in sites:
                if a.id == b.id or is_allowed(a.id, b.id):
                    continue
                for sa in a.subnets:
                    for sb in b.subnets:
                        subprocess.run(["iptables", "-A", chain, "-s", sa.cidr,
                                        "-d", sb.cidr, "-j", "DROP"], capture_output=True)

    # подключить цепочку в начало FORWARD (идемпотентно)
    if subprocess.run(["iptables", "-C", "FORWARD", "-j", chain],
                      capture_output=True).returncode != 0:
        subprocess.run(["iptables", "-I", "FORWARD", "1", "-j", chain], capture_output=True)


def clear_forward_matrix(hub_id: int) -> None:
    chain = fwd_chain(hub_id)
    subprocess.run(["iptables", "-D", "FORWARD", "-j", chain], capture_output=True)
    subprocess.run(["iptables", "-F", chain], capture_output=True)
    subprocess.run(["iptables", "-X", chain], capture_output=True)
