from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr


# ── Auth ──────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class AdminUserCreate(BaseModel):
    username: str
    password: str

class AdminUserOut(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

class AdminPasswordChange(BaseModel):
    new_password: str
    old_password: Optional[str] = None   # требуется при смене своего пароля


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    isp1_host: Optional[str] = None
    isp1_port: Optional[int] = 1194
    isp1_label: Optional[str] = "ISP1"
    isp2_host: Optional[str] = None
    isp2_port: Optional[int] = 1194
    isp2_label: Optional[str] = "ISP2"
    isp3_host: Optional[str] = None
    isp3_port: Optional[int] = 1194
    isp3_label: Optional[str] = "ISP3"
    isp4_host: Optional[str] = None
    isp4_port: Optional[int] = 1194
    isp4_label: Optional[str] = "ISP4"
    server_name: Optional[str] = None
    public_url: Optional[str] = None
    public_urls: Optional[str] = None
    share_ttl_hours: Optional[int] = 72
    share_max_downloads: Optional[int] = 5
    # SMTP
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_tls: Optional[bool] = True
    backup_enabled: Optional[bool] = False
    backup_interval_hours: Optional[int] = 24
    backup_keep: Optional[int] = 7

class SettingsOut(SettingsUpdate):
    id: int
    updated_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class TestEmailRequest(BaseModel):
    to_email: str


# ── CA ────────────────────────────────────────────────────────────────────────

class CACreate(BaseModel):
    common_name: str
    country: str = "RU"
    org: str = "My Company"
    valid_days: int = 3650  # 10 лет

class CAOut(BaseModel):
    id: int
    common_name: str
    created_at: datetime
    expires_at: datetime
    class Config:
        from_attributes = True


# ── VPN Server ────────────────────────────────────────────────────────────────

class ServerCreate(BaseModel):
    name: str
    kind: str = "openvpn"            # openvpn | wireguard
    ca_id: Optional[int] = None      # обязателен только для openvpn
    network: str = "10.8.0.0"
    netmask: str = "255.255.255.0"
    port: int = 1194
    protocol: str = "udp"
    dns_servers: str = "8.8.8.8,8.8.4.4"
    push_routes: str = ""
    obfuscation: bool = False

class ServerUpdate(BaseModel):
    name: Optional[str] = None
    dns_servers: Optional[str] = None
    push_routes: Optional[str] = None
    org_ids: Optional[List[int]] = None   # организации этого сервера

class ServerOut(BaseModel):
    id: int
    name: str
    kind: str = "openvpn"
    ca_id: Optional[int] = None
    network: str
    netmask: str
    port: int
    protocol: str
    dns_servers: str
    push_routes: str
    status: str
    org_ids: List[int] = []
    user_count: int = 0
    obfuscation: bool = False
    created_at: datetime
    class Config:
        from_attributes = True


# ── Organization ──────────────────────────────────────────────────────────────

class OrgCreate(BaseModel):
    name: str
    description: Optional[str] = None

class OrgUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class OrgOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    server_ids: List[int] = []
    user_count: int = 0
    class Config:
        from_attributes = True


# ── VPN User ──────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    org_id: int                          # обязательно — сервер берётся из организации
    server_id: Optional[int] = None      # если у орг несколько серверов — указать явно
    valid_days: int = 365
    password: Optional[str] = None
    no_password: bool = False            # True = ключ без пароля (подключение без запроса)
    notes: Optional[str] = None

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[bool] = None
    org_id: Optional[int] = None
    notes: Optional[str] = None

class UserChangePassword(BaseModel):
    new_password: Optional[str] = None  # None = убрать пароль

class UserReissue(BaseModel):
    valid_days: int = 365
    password: Optional[str] = None  # пароль нового приватного ключа

class UserOut(BaseModel):
    id: int
    username: str
    full_name: Optional[str]
    email: Optional[str]
    server_id: int
    org_id: Optional[int]
    cert_status: str
    cert_expires_at: Optional[datetime]
    cert_password: Optional[str]
    eap_password: Optional[str] = None
    is_active: bool
    archived: bool = False
    notes: Optional[str] = None
    last_connected_at: Optional[datetime] = None
    created_at: datetime
    class Config:
        from_attributes = True

class UserListOut(BaseModel):
    users: List[UserOut]
    total: int


# ── Status ────────────────────────────────────────────────────────────────────

class ConnectedClient(BaseModel):
    common_name: str
    real_address: str
    virtual_address: str
    connected_since: str
    bytes_received: int
    bytes_sent: int

class ServerStatusOut(BaseModel):
    server_id: int
    server_name: str
    status: str
    connected_clients: List[ConnectedClient]
