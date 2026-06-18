"""IPsec (IKEv2) транспорт для site-to-site через strongSwan/swanctl.

Policy-based, аутентификация PSK. Спицы за NAT инициируют подключение к хабу
(remote_addrs=%any). Идентификация по IKE-id (RFC822-вид: spokeN@clickvpn).
Совместимо с MikroTik (RouterOS), Keenetic, аппаратными шлюзами.

На хабе один файл /etc/swanctl/conf.d/clickvpn-s2s-{hub_id}.conf со всеми
IPsec-спицами хаба, пересобирается при каждом применении.
"""
import os
import secrets
import subprocess

from services.s2s.transport import Transport, register
from services.s2s import netutil

SWAN_DIR = "/etc/swanctl"

# Параметры, совместимые с MikroTik/Keenetic «из коробки»
IKE_PROPOSALS = "aes256-sha256-modp2048"
ESP_PROPOSALS = "aes256-sha256-modp2048"


def gen_psk() -> str:
    return secrets.token_urlsafe(32)


def _hub_id_str(hub_site) -> str:
    return f"hub{hub_site.id}@clickvpn"


def _spoke_id_str(spoke) -> str:
    return f"spoke{spoke.id}@clickvpn"


def _conf_path(hub_id: int) -> str:
    return os.path.join(SWAN_DIR, "conf.d", f"clickvpn-s2s-{hub_id}.conf")


def _hub_host(hub_site) -> str:
    """Адрес хаба без порта (из endpoint ip:port)."""
    ep = (hub_site.endpoint or "").strip()
    return ep.split(":")[0] if ep else ""


def _reachable_lans(site_id: int, all_sites: list, is_allowed=None) -> list[str]:
    """LAN-подсети площадок, к которым площадке site_id разрешён доступ."""
    out = []
    for s in all_sites:
        if s.id == site_id:
            continue
        if is_allowed is not None and not is_allowed(site_id, s.id):
            continue
        for sn in s.subnets:
            if sn.cidr not in out:
                out.append(sn.cidr)
    return out


def _conn_block(hub_site, spoke, local_ts: list[str], remote_ts: list[str]) -> str:
    name = f"s2s-{hub_site.id}-{spoke.id}"
    return f"""  {name} {{
    version = 2
    proposals = {IKE_PROPOSALS},default
    local_addrs = %any
    remote_addrs = %any
    mobike = yes
    dpd_delay = 30s
    rekey_time = 4h
    local {{
      auth = psk
      id = {_hub_id_str(hub_site)}
    }}
    remote {{
      auth = psk
      id = {_spoke_id_str(spoke)}
    }}
    children {{
      net {{
        local_ts = {', '.join(local_ts) or '0.0.0.0/0'}
        remote_ts = {', '.join(remote_ts) or '0.0.0.0/0'}
        start_action = none
        dpd_action = clear
        esp_proposals = {ESP_PROPOSALS},default
      }}
    }}
  }}
"""


def _build_hub_conf(hub_site, ipsec_spokes: list, all_sites: list, is_allowed=None) -> str:
    conns = []
    secrets_lines = []
    for spoke in ipsec_spokes:
        if not spoke.psk:
            continue
        spoke_lans = [sn.cidr for sn in spoke.subnets]
        # local_ts = LAN хаба + LAN других площадок, к которым спица имеет доступ
        local_ts = _reachable_lans(spoke.id, all_sites, is_allowed)
        conns.append(_conn_block(hub_site, spoke, local_ts, spoke_lans))
        psk = (spoke.psk or "").replace('"', '\\"')
        secrets_lines.append(
            f'  ike-s2s-{hub_site.id}-{spoke.id} {{ id = {_spoke_id_str(spoke)} secret = "{psk}" }}'
        )

    conf = "connections {\n" + "".join(conns) + "}\n\n"
    conf += "secrets {\n" + "\n".join(secrets_lines) + "\n}\n"
    return conf


def _apply_hub_nat(hub_site, ipsec_spokes: list) -> None:
    """ip_forward + FORWARD accept + NAT только spoke→LAN-хаба.

    NAT строго scoped по dst=LAN хаба: иначе бы маскарадился и межфилиальный
    трафик (он уходит в ESP через eth0), теряя реальные адреса.
    """
    out_if = netutil.default_iface()
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], capture_output=True)
    hub_lans = [sn.cidr for sn in hub_site.subnets]
    for spoke in ipsec_spokes:
        for slan in [sn.cidr for sn in spoke.subnets]:
            # пропускать форвардинг подсети спицы
            for direction in (["-s", slan], ["-d", slan]):
                rule = ["FORWARD", *direction, "-j", "ACCEPT"]
                if subprocess.run(["iptables", "-C", *rule], capture_output=True).returncode != 0:
                    subprocess.run(["iptables", "-A", *rule], capture_output=True)
            # NAT spoke→LAN хаба
            for hlan in hub_lans:
                rule = ["-t", "nat", "POSTROUTING", "-s", slan, "-d", hlan,
                        "-o", out_if, "-j", "MASQUERADE"]
                if subprocess.run(["iptables", "-C", *rule], capture_output=True).returncode != 0:
                    subprocess.run(["iptables", "-A", *rule], capture_output=True)


def _strongswan_service() -> str:
    out = subprocess.run(["systemctl", "list-unit-files"], capture_output=True, text=True).stdout
    for svc in ("strongswan.service", "strongswan-starter.service"):
        if svc in out:
            return svc.replace(".service", "")
    return "strongswan"


def _ensure_running() -> None:
    svc = _strongswan_service()
    if subprocess.run(["systemctl", "is-active", "--quiet", svc],
                      capture_output=True).returncode != 0:
        subprocess.run(["systemctl", "enable", svc], capture_output=True)
        subprocess.run(["systemctl", "restart", svc], capture_output=True)
        import time
        time.sleep(2)  # ждём VICI-сокет


def _build_mikrotik_script(hub_site, spoke, remote_lans: list[str]) -> str:
    """Готовый скрипт RouterOS для филиала-MikroTik."""
    host = _hub_host(hub_site)
    spoke_lans = [sn.cidr for sn in spoke.subnets]
    lines = [
        f"# === Click VPN site-to-site (IPsec/IKEv2) — площадка «{spoke.name}» ===",
        "# Вставить в терминал MikroTik (RouterOS 7).",
        "/ip ipsec profile add name=clickvpn-s2s hash-algorithm=sha256 "
        "enc-algorithm=aes-256 dh-group=modp2048 lifetime=4h",
        "/ip ipsec proposal add name=clickvpn-s2s auth-algorithms=sha256 "
        "enc-algorithms=aes-256-cbc pfs-group=modp2048 lifetime=8h",
        f'/ip ipsec peer add name=clickvpn-s2s address={host}/32 profile=clickvpn-s2s '
        f'exchange-mode=ike2 local-address=0.0.0.0',
        f'/ip ipsec identity add peer=clickvpn-s2s auth-method=pre-shared-key '
        f'secret="{spoke.psk}" my-id=fqdn:{_spoke_id_str(spoke)} '
        f'remote-id=fqdn:{_hub_id_str(hub_site)}',
    ]
    for slan in spoke_lans:
        for rlan in remote_lans:
            lines.append(
                f'/ip ipsec policy add peer=clickvpn-s2s src-address={slan} '
                f'dst-address={rlan} proposal=clickvpn-s2s tunnel=yes'
            )
    return "\n".join(lines) + "\n"


def _build_generic_sheet(hub_site, spoke, remote_lans: list[str]) -> str:
    """Параметры для ручной настройки (Keenetic и пр.)."""
    host = _hub_host(hub_site)
    spoke_lans = [sn.cidr for sn in spoke.subnets]
    return (
        f"# Click VPN site-to-site (IPsec/IKEv2) — площадка «{spoke.name}»\n"
        f"# Параметры туннеля для роутера филиала:\n\n"
        f"Тип             : IPsec IKEv2, туннельный режим\n"
        f"Адрес сервера   : {host}\n"
        f"Аутентификация  : PSK (pre-shared key)\n"
        f"PSK             : {spoke.psk}\n"
        f"Свой ID (my-id) : {_spoke_id_str(spoke)}  (тип FQDN)\n"
        f"ID сервера      : {_hub_id_str(hub_site)}  (тип FQDN)\n\n"
        f"IKE (phase 1)   : AES-256, SHA-256, DH group 14 (modp2048), lifetime 4h\n"
        f"ESP (phase 2)   : AES-256, SHA-256, PFS group 14 (modp2048)\n\n"
        f"Локальные сети (свои): {', '.join(spoke_lans) or '—'}\n"
        f"Удалённые сети       : {', '.join(remote_lans) or '—'}\n"
    )


class IPsecTransport(Transport):
    name = "ipsec"

    def hub_apply(self, hub_site, spoke_sites: list, is_allowed=None) -> tuple[bool, str]:
        all_sites = [hub_site] + list(spoke_sites)
        ipsec_spokes = [s for s in spoke_sites if s.transport == "ipsec"]

        os.makedirs(os.path.join(SWAN_DIR, "conf.d"), exist_ok=True)
        conf = _build_hub_conf(hub_site, ipsec_spokes, all_sites, is_allowed)
        path = _conf_path(hub_site.id)
        with open(path, "w") as f:
            f.write(conf)
        os.chmod(path, 0o600)

        _ensure_running()
        _apply_hub_nat(hub_site, ipsec_spokes)
        r = subprocess.run(["swanctl", "--load-all"], capture_output=True, text=True)
        ok = r.returncode == 0
        msg = (r.stderr or r.stdout).strip()
        return ok, msg or ("OK" if ok else "ошибка swanctl --load-all")

    def hub_teardown(self, hub_site) -> None:
        path = _conf_path(hub_site.id)
        if os.path.exists(path):
            os.remove(path)
        subprocess.run(["swanctl", "--load-all"], capture_output=True)

    def site_config(self, hub_site, site, peer_sites: list | None = None,
                    is_allowed=None) -> str:
        all_sites = (peer_sites or []) + [site]
        remote_lans = _reachable_lans(site.id, all_sites, is_allowed)
        mt = _build_mikrotik_script(hub_site, site, remote_lans)
        sheet = _build_generic_sheet(hub_site, site, remote_lans)
        return (sheet + "\n" + "=" * 60 + "\n"
                + "# Вариант для MikroTik (RouterOS) — команды:\n\n" + mt)

    def status(self, hub_site) -> list[dict]:
        r = subprocess.run(["swanctl", "--list-sas"], capture_output=True, text=True)
        if r.returncode != 0:
            return []
        out = []
        # грубый парс: строки вида "s2s-<hub>-<spoke>: #N, ESTABLISHED, ..."
        prefix = f"s2s-{hub_site.id}-"
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith(prefix) and ":" in line:
                conn = line.split(":")[0]
                try:
                    spoke_id = int(conn.rsplit("-", 1)[1])
                except ValueError:
                    continue
                online = "ESTABLISHED" in line
                out.append({"spoke_id": spoke_id, "online": online})
        return out


register(IPsecTransport())
