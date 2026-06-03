"""IKEv2/IPsec через strongSwan (swanctl). Auth: серверный серт + EAP-MSCHAPv2."""
import os
import subprocess

DATA_DIR = os.getenv("DATA_DIR", "./data")
SWAN_DIR = "/etc/swanctl"


def _default_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"],
                                      stderr=subprocess.DEVNULL, text=True).split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception:
        pass
    return "eth0"


def write_server(server, ca_cert_pem: str, server_cert_pem: str, server_key_pem: str,
                 sans: list[str], eap_users: list[dict], dns: str, pool_cidr: str):
    """Пишет swanctl-конфиг сервера + секреты EAP и применяет.
    eap_users: [{'id': username, 'secret': password}]"""
    for sub in ("x509ca", "x509", "private", "conf.d"):
        os.makedirs(os.path.join(SWAN_DIR, sub), exist_ok=True)

    ca_path = os.path.join(SWAN_DIR, "x509ca", f"clickvpn-ca-{server.id}.pem")
    crt_path = os.path.join(SWAN_DIR, "x509", f"clickvpn-srv-{server.id}.pem")
    key_path = os.path.join(SWAN_DIR, "private", f"clickvpn-srv-{server.id}.pem")
    with open(ca_path, "w") as f: f.write(ca_cert_pem)
    with open(crt_path, "w") as f: f.write(server_cert_pem)
    with open(key_path, "w") as f: f.write(server_key_pem)
    os.chmod(key_path, 0o600)

    server_id_san = sans[0] if sans else "vpn"
    routes = [r.strip() for r in (server.push_routes or "").splitlines() if r.strip()]
    local_ts = ", ".join(routes) if routes else "0.0.0.0/0"
    dns_str = " ".join([d.strip() for d in (dns or "").split(",") if d.strip()])

    conf = f"""connections {{
  clickvpn-{server.id} {{
    version = 2
    proposals = aes256-sha256-modp2048,default
    rekey_time = 0
    pools = clickvpn-pool-{server.id}
    fragmentation = yes
    dpd_delay = 30s
    send_certreq = no
    local {{
      auth = pubkey
      certs = clickvpn-srv-{server.id}.pem
      id = {server_id_san}
    }}
    remote {{
      auth = eap-mschapv2
      eap_id = %any
    }}
    children {{
      net {{
        local_ts = {local_ts}
        rekey_time = 0
        dpd_action = clear
        esp_proposals = aes256-sha256,default
      }}
    }}
  }}
}}

pools {{
  clickvpn-pool-{server.id} {{
    addrs = {pool_cidr}
    dns = {dns_str}
  }}
}}
"""
    secrets = "secrets {\n"
    # ключ серверного сертификата
    secrets += f"  private-srv-{server.id} {{ file = clickvpn-srv-{server.id}.pem }}\n"
    for i, u in enumerate(eap_users):
        pwd = (u["secret"] or "").replace('"', '\\"')
        secrets += f'  eap-{server.id}-{i} {{ id = "{u["id"]}" secret = "{pwd}" }}\n'
    secrets += "}\n"

    conf_path = os.path.join(SWAN_DIR, "conf.d", f"clickvpn-{server.id}.conf")
    with open(conf_path, "w") as f:
        f.write(conf + "\n" + secrets)
    os.chmod(conf_path, 0o600)

    _enable_nat(pool_cidr)
    reload_all()


def _enable_nat(pool_cidr: str):
    iface = _default_iface()
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1")
    except Exception:
        pass
    for args in (
        ["-t", "nat", "-C", "POSTROUTING", "-s", pool_cidr, "-o", iface, "-j", "MASQUERADE"],
    ):
        chk = subprocess.run(["iptables"] + args, capture_output=True)
        if chk.returncode != 0:
            subprocess.run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", pool_cidr,
                            "-o", iface, "-j", "MASQUERADE"], capture_output=True)
    subprocess.run(["iptables", "-C", "FORWARD", "-s", pool_cidr, "-j", "ACCEPT"], capture_output=True).returncode \
        or subprocess.run(["iptables", "-A", "FORWARD", "-s", pool_cidr, "-j", "ACCEPT"], capture_output=True)
    subprocess.run(["iptables", "-C", "FORWARD", "-d", pool_cidr, "-j", "ACCEPT"], capture_output=True).returncode \
        or subprocess.run(["iptables", "-A", "FORWARD", "-d", pool_cidr, "-j", "ACCEPT"], capture_output=True)


def reload_all():
    subprocess.run(["swanctl", "--load-all"], capture_output=True)


def _service_name() -> str:
    out = subprocess.run(["systemctl", "list-unit-files"], capture_output=True, text=True).stdout
    for svc in ("strongswan.service", "strongswan-starter.service"):
        if svc in out:
            return svc.replace(".service", "")
    return "strongswan"


def start():
    svc = _service_name()
    subprocess.run(["systemctl", "enable", svc], capture_output=True)
    subprocess.run(["systemctl", "restart", svc], capture_output=True)
    import time
    time.sleep(2)  # ждём VICI-сокет
    reload_all()


def is_running() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", _service_name()], capture_output=True)
    return r.returncode == 0


def stop_conn(server_id: int):
    conf_path = os.path.join(SWAN_DIR, "conf.d", f"clickvpn-{server_id}.conf")
    if os.path.exists(conf_path):
        os.remove(conf_path)
    reload_all()


def build_mobileconfig(server_name: str, remote_host: str, remote_id: str,
                       username: str, password: str, ca_cert_pem: str) -> str:
    """Генерирует .mobileconfig (iOS/macOS) для IKEv2 EAP."""
    import uuid, base64
    ca_b64 = base64.b64encode(
        _pem_to_der(ca_cert_pem)
    ).decode()
    u_vpn = str(uuid.uuid4()).upper()
    u_ca = str(uuid.uuid4()).upper()
    u_prof = str(uuid.uuid4()).upper()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>PayloadContent</key><array>
    <dict>
      <key>PayloadType</key><string>com.apple.security.root</string>
      <key>PayloadIdentifier</key><string>com.clickvpn.ca.{u_ca}</string>
      <key>PayloadUUID</key><string>{u_ca}</string>
      <key>PayloadVersion</key><integer>1</integer>
      <key>PayloadDisplayName</key><string>{server_name} CA</string>
      <key>PayloadContent</key><data>{ca_b64}</data>
    </dict>
    <dict>
      <key>PayloadType</key><string>com.apple.vpn.managed</string>
      <key>PayloadIdentifier</key><string>com.clickvpn.vpn.{u_vpn}</string>
      <key>PayloadUUID</key><string>{u_vpn}</string>
      <key>PayloadVersion</key><integer>1</integer>
      <key>PayloadDisplayName</key><string>{server_name}</string>
      <key>UserDefinedName</key><string>{server_name}</string>
      <key>VPNType</key><string>IKEv2</string>
      <key>IKEv2</key><dict>
        <key>RemoteAddress</key><string>{remote_host}</string>
        <key>RemoteIdentifier</key><string>{remote_id}</string>
        <key>LocalIdentifier</key><string>{username}</string>
        <key>AuthenticationMethod</key><string>None</string>
        <key>ExtendedAuthEnabled</key><integer>1</integer>
        <key>AuthName</key><string>{username}</string>
        <key>AuthPassword</key><string>{password}</string>
        <key>ServerCertificateIssuerCommonName</key><string></string>
        <key>IKESecurityAssociationParameters</key><dict>
          <key>EncryptionAlgorithm</key><string>AES-256</string>
          <key>IntegrityAlgorithm</key><string>SHA2-256</string>
          <key>DiffieHellmanGroup</key><integer>14</integer>
        </dict>
        <key>ChildSecurityAssociationParameters</key><dict>
          <key>EncryptionAlgorithm</key><string>AES-256</string>
          <key>IntegrityAlgorithm</key><string>SHA2-256</string>
          <key>DiffieHellmanGroup</key><integer>14</integer>
        </dict>
      </dict>
    </dict>
  </array>
  <key>PayloadDisplayName</key><string>{server_name} VPN</string>
  <key>PayloadIdentifier</key><string>com.clickvpn.profile.{u_prof}</string>
  <key>PayloadUUID</key><string>{u_prof}</string>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict></plist>
"""


def _pem_to_der(pem: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    cert = x509.load_pem_x509_certificate(pem.encode(), default_backend())
    return cert.public_bytes(serialization.Encoding.DER)
