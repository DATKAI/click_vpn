"""PKI service — генерация CA, серверных и клиентских сертификатов."""
import os
import ipaddress
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, dh
from cryptography.hazmat.backends import default_backend


def _utcnow():
    return datetime.now(timezone.utc)


def generate_key(key_size: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend(),
    )


def key_to_pem(key: rsa.RSAPrivateKey, password: str | None = None) -> str:
    if password:
        encryption = serialization.BestAvailableEncryption(password.encode())
    else:
        encryption = serialization.NoEncryption()
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=encryption,
    ).decode()


def cert_to_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def create_ca(
    common_name: str,
    country: str = "RU",
    org: str = "My Company",
    valid_days: int = 3650,
) -> tuple[str, str, datetime]:
    """Возвращает (cert_pem, key_pem, expires_at)."""
    key = generate_key(4096)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    now = _utcnow()
    expires_at = now + timedelta(days=valid_days)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256(), default_backend())
    )
    return cert_to_pem(cert), key_to_pem(key), expires_at


def create_server_cert(
    ca_cert_pem: str,
    ca_key_pem: str,
    serial: int,
    common_name: str,
    valid_days: int = 3650,
) -> tuple[str, str, datetime]:
    """Серверный сертификат (tls-server). Возвращает (cert_pem, key_pem, expires_at)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode(), default_backend())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None, backend=default_backend())

    key = generate_key()
    now = _utcnow()
    expires_at = now + timedelta(days=valid_days)

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    return cert_to_pem(cert), key_to_pem(key), expires_at


def create_client_cert(
    ca_cert_pem: str,
    ca_key_pem: str,
    serial: int,
    common_name: str,
    valid_days: int = 365,
    password: str | None = None,
) -> tuple[str, str, datetime]:
    """Клиентский сертификат. Возвращает (cert_pem, key_pem, expires_at)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode(), default_backend())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None, backend=default_backend())

    key = generate_key()
    now = _utcnow()
    expires_at = now + timedelta(days=valid_days)

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256(), default_backend())
    )
    return cert_to_pem(cert), key_to_pem(key, password=password), expires_at


def create_ikev2_server_cert(
    ca_cert_pem: str, ca_key_pem: str, serial: int,
    common_name: str, sans: list[str], valid_days: int = 3650,
) -> tuple[str, str, datetime]:
    """Серверный сертификат для IKEv2 с SubjectAltName (IP/DNS)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode(), default_backend())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None, backend=default_backend())
    key = generate_key()
    now = _utcnow()
    expires_at = now + timedelta(days=valid_days)

    san_objs = []
    for s in sans:
        try:
            san_objs.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            san_objs.append(x509.DNSName(s))

    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(expires_at)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    )
    if san_objs:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_objs), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256(), default_backend())
    return cert_to_pem(cert), key_to_pem(key), expires_at


def generate_tls_crypt_key() -> str:
    """Генерирует статический ключ OpenVPN (формат tls-crypt/tls-auth), 2048 бит."""
    import binascii
    data = os.urandom(256)
    hexstr = binascii.hexlify(data).decode()
    lines = [hexstr[i:i + 32] for i in range(0, len(hexstr), 32)]
    body = "\n".join(lines)
    return ("-----BEGIN OpenVPN Static key V1-----\n"
            + body + "\n-----END OpenVPN Static key V1-----\n")


def generate_dh_params(key_size: int = 2048) -> str:
    """Генерирует DH параметры для OpenVPN сервера."""
    parameters = dh.generate_parameters(
        generator=2,
        key_size=key_size,
        backend=default_backend()
    )
    return parameters.parameter_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.ParameterFormat.PKCS3
    ).decode()


def build_crl(
    ca_cert_pem: str,
    ca_key_pem: str,
    revoked_serials: list[int],
) -> str:
    """Генерирует CRL (список отозванных сертификатов)."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode(), default_backend())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None, backend=default_backend())

    now = _utcnow()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_cert.subject)
        .last_update(now)
        .next_update(now + timedelta(days=7))
    )
    for serial in revoked_serials:
        revoked = (
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(now)
            .build(default_backend())
        )
        builder = builder.add_revoked_certificate(revoked)

    crl = builder.sign(private_key=ca_key, algorithm=hashes.SHA256(), backend=default_backend())
    return crl.public_bytes(serialization.Encoding.PEM).decode()
