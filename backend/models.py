from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    ForeignKey, Text, Enum, Table
)
from sqlalchemy.orm import relationship
import enum

from database import Base


class ServerStatus(str, enum.Enum):
    stopped = "stopped"
    running = "running"
    error = "error"


class CertStatus(str, enum.Enum):
    active = "active"
    revoked = "revoked"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Settings(Base):
    """Singleton row — глобальные настройки сервиса."""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)
    isp1_host = Column(String(256), nullable=True)
    isp1_port = Column(Integer, default=1194)
    isp1_label = Column(String(64), default="ISP1")
    isp2_host = Column(String(256), nullable=True)
    isp2_port = Column(Integer, default=1194)
    isp2_label = Column(String(64), default="ISP2")
    isp3_host = Column(String(256), nullable=True)
    isp3_port = Column(Integer, default=1194)
    isp3_label = Column(String(64), default="ISP3")
    isp4_host = Column(String(256), nullable=True)
    isp4_port = Column(Integer, default=1194)
    isp4_label = Column(String(64), default="ISP4")
    server_name = Column(String(128), default="VPN Server")
    # SMTP
    smtp_host = Column(String(256), nullable=True)
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String(256), nullable=True)
    smtp_password = Column(String(256), nullable=True)
    smtp_from = Column(String(256), nullable=True)
    smtp_tls = Column(Boolean, default=True)
    # Авто-бэкап
    backup_enabled = Column(Boolean, default=False)
    backup_interval_hours = Column(Integer, default=24)
    backup_keep = Column(Integer, default=7)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CA(Base):
    """Корневой удостоверяющий центр."""
    __tablename__ = "ca"

    id = Column(Integer, primary_key=True)
    common_name = Column(String(256), nullable=False)
    cert_pem = Column(Text, nullable=False)
    key_pem = Column(Text, nullable=False)
    serial = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    servers = relationship("VPNServer", back_populates="ca")
    users = relationship("VPNUser", back_populates="ca")


# Many-to-many: Organization <-> VPNServer
org_server = Table(
    "org_server", Base.metadata,
    Column("org_id", Integer, ForeignKey("organizations.id"), primary_key=True),
    Column("server_id", Integer, ForeignKey("vpn_servers.id"), primary_key=True),
)


class Organization(Base):
    """Организация / филиал / отдел."""
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, unique=True)
    description = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("VPNUser", back_populates="organization")
    servers = relationship("VPNServer", secondary=org_server, back_populates="organizations")


class VPNServer(Base):
    """OpenVPN сервер."""
    __tablename__ = "vpn_servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), default="openvpn")   # openvpn | wireguard
    ca_id = Column(Integer, ForeignKey("ca.id"), nullable=True)  # WG не нужен CA

    # WireGuard серверные ключи
    wg_private_key = Column(Text, nullable=True)
    wg_public_key = Column(Text, nullable=True)

    network = Column(String(64), default="10.8.0.0")
    netmask = Column(String(64), default="255.255.255.0")
    port = Column(Integer, default=1194)
    protocol = Column(String(8), default="udp")
    dns_servers = Column(String(256), default="8.8.8.8,8.8.4.4")
    push_routes = Column(Text, default="")

    obfuscation = Column(Boolean, default=False)   # TCP/443 + tls-crypt (обход DPI)
    tls_crypt_key = Column(Text, nullable=True)    # статический ключ tls-crypt

    status = Column(Enum(ServerStatus), default=ServerStatus.stopped)
    config_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    ca = relationship("CA", back_populates="servers")
    users = relationship("VPNUser", back_populates="server")
    organizations = relationship("Organization", secondary=org_server, back_populates="servers")


class VPNUser(Base):
    """Пользователь / клиент VPN."""
    __tablename__ = "vpn_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(128), nullable=False)
    full_name = Column(String(256), nullable=True)   # ФИО
    email = Column(String(256), nullable=True)
    ca_id = Column(Integer, ForeignKey("ca.id"), nullable=False)
    server_id = Column(Integer, ForeignKey("vpn_servers.id"), nullable=False)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)

    # Сертификат
    cert_pem = Column(Text, nullable=True)
    key_pem = Column(Text, nullable=True)
    cert_serial = Column(Integer, nullable=True)
    cert_status = Column(Enum(CertStatus), default=CertStatus.active)
    cert_expires_at = Column(DateTime, nullable=True)
    cert_password = Column(String(256), nullable=True)  # пароль приватного ключа

    # WireGuard
    wg_private_key = Column(Text, nullable=True)
    wg_public_key = Column(Text, nullable=True)
    wg_address = Column(String(64), nullable=True)

    is_active = Column(Boolean, default=True)     # доступ включён/выключен
    archived = Column(Boolean, default=False)     # в архиве (скрыт)
    notes = Column(Text, nullable=True)           # комментарий админа
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)
    last_connected_at = Column(DateTime, nullable=True)  # последнее подключение

    ca = relationship("CA", back_populates="users")
    server = relationship("VPNServer", back_populates="users")
    organization = relationship("Organization", back_populates="users")


class RevokedSerial(Base):
    """Серийники отозванных/удалённых сертификатов — для построения CRL."""
    __tablename__ = "revoked_serials"

    id = Column(Integer, primary_key=True)
    ca_id = Column(Integer, ForeignKey("ca.id"), nullable=False)
    serial = Column(Integer, nullable=False)
    revoked_at = Column(DateTime, default=datetime.utcnow)


class ConnectionLog(Base):
    """История подключений клиентов."""
    __tablename__ = "connection_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("vpn_users.id"), nullable=True)
    common_name = Column(String(128), nullable=False)
    server_id = Column(Integer, nullable=True)
    real_address = Column(String(64), nullable=True)
    virtual_address = Column(String(64), nullable=True)
    connected_at = Column(DateTime, default=datetime.utcnow)
    disconnected_at = Column(DateTime, nullable=True)
    bytes_received = Column(Integer, default=0)
    bytes_sent = Column(Integer, default=0)


class TrafficSample(Base):
    """Временной ряд трафика: дельты за интервал (для графиков)."""
    __tablename__ = "traffic_samples"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    server_id = Column(Integer, nullable=True)   # None = глобально
    rx = Column(Integer, default=0)              # принято байт за интервал
    tx = Column(Integer, default=0)              # отдано байт за интервал
    online = Column(Integer, default=0)          # пик онлайн за интервал


class AuditLog(Base):
    """Журнал действий администраторов."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    admin = Column(String(64), nullable=True)
    action = Column(String(64), nullable=False)   # login, user.create, user.delete...
    target = Column(String(256), nullable=True)
    details = Column(String(512), nullable=True)
