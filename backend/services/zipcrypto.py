"""Создание ZIP-архива с traditional PKWARE-шифрованием (ZipCrypto).

Зачем: почтовые серверы режут .exe вложения. Зашифрованный архив фильтр
прочитать не может → письмо проходит. ZipCrypto (legacy) открывается
встроенным проводником Windows — получателю не нужен 7-Zip/WinRAR.

Без внешних зависимостей. ZipCrypto криптографически слаб, но здесь он
нужен лишь чтобы обойти антивирус-сканеры почты, а не как защита секрета
(сам .exe защиты не требует — это публичный инсталлятор + профиль).
"""
import os
import zlib
import struct
import secrets


def _crc_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
        table.append(c & 0xFFFFFFFF)
    return table


_TABLE = _crc_table()


def _crc32_step(crc, b):
    return ((crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]) & 0xFFFFFFFF


class _Cipher:
    """Потоковый шифр PKWARE."""
    __slots__ = ("k0", "k1", "k2")

    def __init__(self, password: bytes):
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for b in password:
            self._update(b)

    def _update(self, b):
        self.k0 = _crc32_step(self.k0, b)
        self.k1 = (self.k1 + (self.k0 & 0xFF)) & 0xFFFFFFFF
        self.k1 = (self.k1 * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = _crc32_step(self.k2, (self.k1 >> 24) & 0xFF)

    def _decrypt_byte(self):
        temp = (self.k2 | 2) & 0xFFFF
        return ((temp * (temp ^ 1)) >> 8) & 0xFF

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        # локальные алиасы для скорости
        table = _TABLE
        k0, k1, k2 = self.k0, self.k1, self.k2
        for i, p in enumerate(data):
            temp = (k2 | 2) & 0xFFFF
            out[i] = p ^ (((temp * (temp ^ 1)) >> 8) & 0xFF)
            k0 = ((k0 >> 8) ^ table[(k0 ^ p) & 0xFF]) & 0xFFFFFFFF
            k1 = (k1 + (k0 & 0xFF)) & 0xFFFFFFFF
            k1 = (k1 * 134775813 + 1) & 0xFFFFFFFF
            k2 = ((k2 >> 8) ^ table[(k2 ^ ((k1 >> 24) & 0xFF)) & 0xFF]) & 0xFFFFFFFF
        self.k0, self.k1, self.k2 = k0, k1, k2
        return bytes(out)


def make_encrypted_zip(entries: list[tuple[str, bytes]], password: str) -> bytes:
    """entries: [(имя_файла, содержимое_байты)]. Возвращает байты ZIP-архива
    с traditional-шифрованием (flag bit 0). Совместим с проводником Windows."""
    pw = password.encode("utf-8")
    out = bytearray()
    central = bytearray()
    offsets = []

    for name, data in entries:
        name_b = name.encode("utf-8")
        crc = zlib.crc32(data) & 0xFFFFFFFF

        # сжатие (raw deflate)
        comp = zlib.compressobj(6, zlib.DEFLATED, -15)
        compressed = comp.compress(data) + comp.flush()

        # 12-байтный заголовок шифрования: 11 случайных + проверочный (старший байт CRC)
        header = bytearray(secrets.token_bytes(12))
        header[11] = (crc >> 24) & 0xFF

        cipher = _Cipher(pw)
        enc = cipher.encrypt(bytes(header) + compressed)

        comp_size = len(enc)
        uncomp_size = len(data)
        flag = 0x0001  # bit0: encrypted
        method = 8     # deflate
        dostime = 0
        dosdate = 0x21  # 1980-01-01

        local_off = len(out)
        offsets.append(local_off)

        # Local file header
        out += struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, flag, method, dostime, dosdate,
            crc, comp_size, uncomp_size, len(name_b), 0,
        )
        out += name_b
        out += enc

        # Central directory record
        central += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, flag, method, dostime, dosdate,
            crc, comp_size, uncomp_size, len(name_b), 0, 0, 0, 0, 0,
            local_off,
        )
        central += name_b

    cd_offset = len(out)
    out += central
    cd_size = len(central)

    # End of central directory
    out += struct.pack(
        "<IHHHHIIH",
        0x06054B50, 0, 0, len(entries), len(entries), cd_size, cd_offset, 0,
    )
    return bytes(out)
