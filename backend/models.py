from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
    ForeignKey, Text, Enum
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
    # Провайдер 1
    isp1_host = Column(String(256), nullable=True)
    isp1_port = Column(Integer, default=1194)
    isp1_label = Column(String(64), default="ISP1")
    # Провайдер 2
    isp2_host = Column(String(256), nullable=True)
    isp2_port = Column(Integer, default=1194)
    isp2_label = Column(String(64), default="ISP2")
    # Общие
    server_name = Column(String(128), default="VPN Server")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CA(Base):
    """Корневой удостоверяющий центр."""
    __tablename__ = "ca"

    id = Column(Integer, primary_key=True)
    common_name = Column(String(256), nullable=False)
    cert_pem = Column(Text, nullable=False)
    key_pem = Column(Text, nullable=False)  # хранится зашифрованным
    serial = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    servers = relationship("VPNServer", back_populates="ca")
    users = relationship("VPNUser", back_populates="ca")


class VPNServer(Base):
    """OpenVPN сервер (один сервис = один процесс openvpn)."""
    __tablename__ = "vpn_servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    ca_id = Column(Integer, ForeignKey("ca.id"), nullable=False)

    # Сеть
    network = Column(String(64), default="10.8.0.0")
    netmask = Column(String(64), default="255.255.255.0")
    port = Column(Integer, default=1194)
    protocol = Column(String(8), default="udp")  # udp / tcp

    # DNS, маршруты
    dns_servers = Column(String(256), default="8.8.8.8,8.8.4.4")
    push_routes = Column(Text, default="")  # newline-separated CIDR

    # Состояние
    status = Column(Enum(ServerStatus), default=ServerStatus.stopped)
    config_path = Column(String(512), nullable=True)  # путь к .conf файлу
    created_at = Column(DateTime, default=datetime.utcnow)

    ca = relationship("CA", back_populates="servers")
    users = relationship("VPNUser", back_populates="server")


class VPNUser(Base):
    """Пользователь / клиент VPN."""
    __tablename__ = "vpn_users"

    id = Column(Integer, primary_key=True)
    username = Column(String(128), nullable=False)
    email = Column(String(256), nullable=True)
    ca_id = Column(Integer, ForeignKey("ca.id"), nullable=False)
    server_id = Column(Integer, ForeignKey("vpn_servers.id"), nullable=False)

    # Сертификат
    cert_pem = Column(Text, nullable=True)
    key_pem = Column(Text, nullable=True)
    cert_serial = Column(Integer, nullable=True)
    cert_status = Column(Enum(CertStatus), default=CertStatus.active)
    cert_expires_at = Column(DateTime, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)

    ca = relationship("CA", back_populates="users")
    server = relationship("VPNServer", back_populates="users")
