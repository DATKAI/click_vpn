"""Отправка email с вложением .ovpn через SMTP."""
import smtplib
import ssl
from email.message import EmailMessage


def send_ovpn_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str | None,
    smtp_password: str | None,
    smtp_from: str,
    smtp_tls: bool,
    to_email: str,
    client_name: str,
    ovpn_content: str,
    server_name: str = "VPN",
) -> None:
    """Отправляет .ovpn файл вложением. Бросает исключение при ошибке."""
    msg = EmailMessage()
    msg["Subject"] = f"Доступ к {server_name} — настройка VPN"
    msg["From"] = smtp_from
    msg["To"] = to_email

    body = f"""Здравствуйте, {client_name}!

Во вложении файл конфигурации для подключения к корпоративному VPN.

Как подключиться:
1. Установите приложение OpenVPN Connect:
   • Windows / macOS: https://openvpn.net/client/
   • Android: Google Play «OpenVPN Connect»
   • iOS: App Store «OpenVPN Connect»
2. Откройте вложенный файл .ovpn в приложении (или импортируйте его).
3. Нажмите «Подключиться».

Файл конфигурации содержит ваш персональный ключ — не передавайте его другим.

С уважением,
Служба поддержки {server_name}
"""
    msg.set_content(body)

    filename = f"{client_name}.ovpn"
    msg.add_attachment(
        ovpn_content.encode("utf-8"),
        maintype="application",
        subtype="x-openvpn-profile",
        filename=filename,
    )

    if smtp_tls and smtp_port == 465:
        # SSL (implicit)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=20) as server:
            if smtp_user:
                server.login(smtp_user, smtp_password or "")
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            if smtp_tls:
                server.starttls(context=ssl.create_default_context())
            if smtp_user:
                server.login(smtp_user, smtp_password or "")
            server.send_message(msg)


def send_test_email(
    smtp_host: str, smtp_port: int, smtp_user: str | None,
    smtp_password: str | None, smtp_from: str, smtp_tls: bool,
    to_email: str, server_name: str = "VPN",
) -> None:
    """Тестовое письмо для проверки SMTP."""
    msg = EmailMessage()
    msg["Subject"] = f"Тест SMTP — {server_name}"
    msg["From"] = smtp_from
    msg["To"] = to_email
    msg.set_content("Это тестовое письмо. Настройки SMTP работают корректно ✅")

    if smtp_tls and smtp_port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=20) as server:
            if smtp_user:
                server.login(smtp_user, smtp_password or "")
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            if smtp_tls:
                server.starttls(context=ssl.create_default_context())
            if smtp_user:
                server.login(smtp_user, smtp_password or "")
            server.send_message(msg)
