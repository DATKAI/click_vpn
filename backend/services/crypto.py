"""Прозрачное шифрование секретов в БД (приватные ключи, пароли).

- Симметричный Fernet (AES-128-CBC + HMAC-SHA256) из библиотеки cryptography.
- Мастер-ключ берётся из DB_ENCRYPTION_KEY, иначе из SECRET_KEY (.env).
- Зашифрованные значения помечаются префиксом PREFIX → можно безопасно
  смешивать со старыми открытыми значениями и мигрировать постепенно.

ВАЖНО: если мастер-ключ изменится, ранее зашифрованные данные станут
нечитаемыми. SECRET_KEY/DB_ENCRYPTION_KEY менять нельзя после включения.
"""
import os
import base64
import hashlib
import functools

from sqlalchemy.types import TypeDecorator, Text

PREFIX = "enc:v1:"


@functools.lru_cache(maxsize=1)
def _fernet():
    from cryptography.fernet import Fernet
    secret = os.getenv("DB_ENCRYPTION_KEY") or os.getenv("SECRET_KEY") or "click-vpn-default-insecure-key"
    # derive 32-байтный ключ из секрета произвольной длины
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext):
    """str → 'enc:v1:<token>'. None и уже зашифрованное возвращаются как есть."""
    if plaintext is None:
        return None
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    if plaintext.startswith(PREFIX):
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value):
    """'enc:v1:<token>' → str. Значения без префикса (старые) возвращаются как есть."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value  # ещё не зашифровано (плавная миграция)
    token = value[len(PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        # неверный ключ/повреждение — возвращаем как есть, чтобы не ронять приложение
        return value


class EncryptedText(TypeDecorator):
    """Колонка Text, прозрачно шифруемая при записи и расшифровываемая при чтении."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
