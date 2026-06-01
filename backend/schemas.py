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


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    isp1_host: Optional[str] = None
    isp1_port: Optional[int] = 1194
    isp1_label: Optional[str] = "ISP1"
    isp2_host: Optional[str] = None
    isp2_port: Optional[int] = 1194
    isp2_label: Optional[str] = "ISP2"
    server_name: Optional[str] = None

class SettingsOut(SettingsUpdate):
    id: int
    updated_at: Optional[datetime] = None
    class Config:
        from_attributes = True


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
    ca_id: int
    network: str = "10.8.0.0"
    netmask: str = "255.255.255.0"
    port: int = 1194
    protocol: str = "udp"
    dns_servers: str = "8.8.8.8,8.8.4.4"
    push_routes: str = ""

class ServerOut(BaseModel):
    id: int
    name: str
    ca_id: int
    network: str
    netmask: str
    port: int
    protocol: str
    dns_servers: str
    push_routes: str
    status: str
    created_at: datetime
    class Config:
        from_attributes = True


# ── VPN User ──────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    server_id: int
    valid_days: int = 365

class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[str]
    server_id: int
    cert_status: str
    cert_expires_at: Optional[datetime]
    is_active: bool
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
