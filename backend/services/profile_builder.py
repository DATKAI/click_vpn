"""Сборка .ovpn профиля для клиента с двумя ISP хостами."""


def build_ovpn_profile(
    ca_cert_pem: str,
    client_cert_pem: str,
    client_key_pem: str,
    isps: list[dict],   # [{"host": ..., "port": ..., "label": ...}, ...]
    protocol: str = "udp",
    tls_auth_key: str | None = None,
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

    if tls_auth_key:
        lines += [
            "",
            "key-direction 1",
            "<tls-auth>",
            tls_auth_key.strip(),
            "</tls-auth>",
        ]

    return "\n".join(lines) + "\n"


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
) -> str:
    dns_list = [d.strip() for d in dns_servers.split(",") if d.strip()]
    dns_lines = "\n".join(f'push "dhcp-option DNS {d}"' for d in dns_list)

    route_lines = ""
    if push_routes:
        for route in push_routes.strip().splitlines():
            route = route.strip()
            if route:
                try:
                    import ipaddress
                    net = ipaddress.ip_network(route, strict=False)
                    route_lines += f'push "route {net.network_address} {net.netmask}"\n'
                except ValueError:
                    pass

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

user nobody
group nogroup
persist-key
persist-tun

status {status_path} 10
verb 3

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

    if tls_auth_key:
        config += f"""
key-direction 0
<tls-auth>
{tls_auth_key.strip()}
</tls-auth>
"""

    config += f"\n{dns_lines}\n{route_lines}"
    return config
