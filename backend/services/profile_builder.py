"""Сборка .ovpn профиля для клиента с двумя ISP хостами."""


def build_ovpn_profile(
    ca_cert_pem: str,
    client_cert_pem: str,
    client_key_pem: str,
    isps: list[dict],   # [{"host": ..., "port": ..., "label": ...}, ...]
    protocol: str = "udp",
    tls_auth_key: str | None = None,
    tls_crypt_key: str | None = None,
) -> str:
    # Фильтруем только заполненные провайдеры
    active_isps = [i for i in isps if i.get("host")]

    lines = [
        "client",
        "dev tun",
        f"proto {protocol}",
        "",
    ]

    for isp in active_isps:
        lines.append(f"# {isp.get('label', 'ISP')}")
        lines.append(f"remote {isp['host']} {isp['port']}")

    if len(active_isps) > 1:
        lines.append("remote-random-hostname")

    lines += [
        "",
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "",
        "# Безопасность",
        "tls-client",
        "remote-cert-tls server",
        "cipher AES-256-GCM",
        "auth SHA256",
        "tls-version-min 1.2",
        "",
        "verb 3",
        "connect-retry 5",
        "connect-retry-max 3",
        "",
        "<ca>",
        ca_cert_pem.strip(),
        "</ca>",
        "",
        "<cert>",
        client_cert_pem.strip(),
        "</cert>",
        "",
        "<key>",
        client_key_pem.strip(),
        "</key>",
    ]

    if tls_crypt_key:
        lines += [
            "",
            "<tls-crypt>",
            tls_crypt_key.strip(),
            "</tls-crypt>",
        ]
    elif tls_auth_key:
        lines += [
            "",
            "key-direction 1",
            "<tls-auth>",
            tls_auth_key.strip(),
            "</tls-auth>",
        ]

    return "\n".join(lines) + "\n"


def _build_push_lines(dns_servers: str, push_routes: str):
    """Возвращает (dns_lines, route_lines) для server-конфига OpenVPN."""
    import ipaddress
    dns_list = [d.strip() for d in (dns_servers or "").split(",") if d.strip()]
    dns_lines = "\n".join(f'push "dhcp-option DNS {d}"' for d in dns_list)
    route_lines = ""
    if push_routes:
        for route in push_routes.strip().splitlines():
            route = route.strip()
            if not route:
                continue
            try:
                net = ipaddress.ip_network(route, strict=False)
                route_lines += f'push "route {net.network_address} {net.netmask}"\n'
            except ValueError:
                pass
    return dns_lines, route_lines


def rewrite_pushes(config_path: str, dns_servers: str, push_routes: str) -> bool:
    """Обновляет push route/DNS в существующем server-конфиге без перевыпуска
    сертификатов. Возвращает True при успехе."""
    if not config_path or not __import__("os").path.exists(config_path):
        return False
    try:
        with open(config_path) as f:
            lines = f.readlines()
    except OSError:
        return False
    # выкидываем старые push route / push dhcp-option DNS
    kept = [ln for ln in lines
            if not (ln.lstrip().startswith('push "route ')
                    or ln.lstrip().startswith('push "dhcp-option DNS '))]
    dns_lines, route_lines = _build_push_lines(dns_servers, push_routes)
    body = "".join(kept).rstrip("\n") + "\n"
    if dns_lines:
        body += dns_lines + "\n"
    if route_lines:
        body += route_lines
    try:
        with open(config_path, "w") as f:
            f.write(body)
        return True
    except OSError:
        return False


def build_server_config(
    server_id: int,
    ca_cert_pem: str,
    server_cert_pem: str,
    server_key_pem: str,
    dh_pem: str,
    network: str,
    netmask: str,
    port: int,
    protocol: str,
    dns_servers: str,
    push_routes: str,
    crl_path: str,
    data_dir: str,
    tls_auth_key: str | None = None,
    tls_crypt_key: str | None = None,
) -> str:
    dns_lines, route_lines = _build_push_lines(dns_servers, push_routes)

    status_path = f"{data_dir}/openvpn/status_{server_id}.log"

    config = f"""# OpenVPN server config — server_id={server_id}
port {port}
proto {protocol}
dev tun{server_id}

server {network} {netmask}
ifconfig-pool-persist {data_dir}/openvpn/ipp_{server_id}.txt
topology subnet

keepalive 10 120
cipher AES-256-GCM
auth SHA256
tls-version-min 1.2
tls-server

# работаем от root — нужно для записи status-файла и управления маршрутами
persist-key
persist-tun

status {status_path} 10
verb 3

# management-интерфейс для мгновенного разрыва сессий
management {data_dir}/openvpn/mgmt_{server_id}.sock unix

crl-verify {crl_path}

<ca>
{ca_cert_pem.strip()}
</ca>
<cert>
{server_cert_pem.strip()}
</cert>
<key>
{server_key_pem.strip()}
</key>
<dh>
{dh_pem.strip()}
</dh>
"""

    if tls_crypt_key:
        config += f"""
<tls-crypt>
{tls_crypt_key.strip()}
</tls-crypt>
"""
    elif tls_auth_key:
        config += f"""
key-direction 0
<tls-auth>
{tls_auth_key.strip()}
</tls-auth>
"""

    config += f"\n{dns_lines}\n{route_lines}"
    return config
