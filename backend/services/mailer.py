"""Отправка email с вложениями (.ovpn / .exe) и инструкцией через SMTP."""
import smtplib
import ssl
import html as _html
from email.message import EmailMessage


def _send(msg: EmailMessage, smtp_host, smtp_port, smtp_user, smtp_password, smtp_tls):
    """Общая отправка письма (SSL/STARTTLS)."""
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


def _instructions_html(kind: str, has_installer: bool, password: str | None,
                       archive_password: str | None = None) -> str:
    """HTML-инструкция под тип клиента и вложения."""
    pwd_block = ""
    if password:
        pwd_block = f"""
        <div style="margin:16px 0;padding:14px 16px;background:#fef9c3;border:1px solid #fde68a;border-radius:10px">
          <div style="font-size:13px;color:#92400e;margin-bottom:4px">🔑 Пароль для подключения к VPN</div>
          <div style="font-family:monospace;font-size:18px;font-weight:700;color:#1e293b;letter-spacing:1px">{_html.escape(password)}</div>
          <div style="font-size:12px;color:#a16207;margin-top:6px">Вводится при подключении OpenVPN. Никому его не сообщайте.</div>
        </div>"""

    arc_block = ""
    if archive_password:
        arc_block = f"""
        <div style="margin:16px 0;padding:14px 16px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px">
          <div style="font-size:13px;color:#1e40af;margin-bottom:4px">📦 Пароль от архива (.zip)</div>
          <div style="font-family:monospace;font-size:18px;font-weight:700;color:#1e293b;letter-spacing:1px">{_html.escape(archive_password)}</div>
          <div style="font-size:12px;color:#3b82f6;margin-top:6px">Нужен только чтобы распаковать вложенный архив с установщиком.</div>
        </div>"""

    if kind == "openvpn" and has_installer:
        unzip_step = (
            f"<li><b>Распакуйте архив</b> <code>.zip</code> из вложения: правый клик → «Извлечь всё». "
            f"Введите <b>пароль от архива</b> (см. ниже).</li>" if archive_password else ""
        )
        steps = f"""
        <ol style="font-size:14px;color:#334155;line-height:1.8;padding-left:20px">
          {unzip_step}
          <li><b>Запустите установщик</b> <code>ClickVPN-…-setup.exe</code>. На вопрос системы о правах нажмите «Да».</li>
          <li>Дождитесь сообщения «Click VPN установлен!» и нажмите «ОК».</li>
          <li>На рабочем столе появится ярлык <b>«Click VPN»</b> — запустите его. Внизу экрана, в <b>области уведомлений</b> (справа от часов, иногда под стрелкой ▲), появится значок OpenVPN — серый экран с замком.</li>
          <li><b>Нажмите на значок правой кнопкой мыши</b> → <b>«Подключиться»</b> (Connect).{(" Введите пароль из этого письма и поставьте галочку «Запомнить» — тогда вводить пароль каждый раз не придётся." if password else "")}</li>
          <li>Когда значок станет <b>зелёным</b> — VPN подключён. Чтобы отключиться: правый клик по значку → «Отключиться».</li>
        </ol>
        {arc_block}
        {pwd_block}
        <div style="margin-top:14px;padding:12px 14px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;font-size:13px;color:#1e40af">
          💡 <b>Чтобы не вводить пароль каждый раз:</b> при подключении отметьте галочку <b>«Запомнить»</b> в окне ввода пароля.<br>
          💡 <b>Чтобы значок OpenVPN всегда был виден:</b> нажмите на стрелку ▲ в трее и перетащите значок OpenVPN на панель задач.
        </div>"""
    elif kind == "openvpn":
        steps = f"""
        <ol style="font-size:14px;color:#334155;line-height:1.8;padding-left:20px">
          <li>Установите <b>OpenVPN GUI</b> с сайта <a href="https://openvpn.net/community-downloads/">openvpn.net</a> (или используйте установщик, если он во вложении).</li>
          <li>Импортируйте вложенный файл <code>.ovpn</code>: правый клик по значку OpenVPN в трее → «Импорт файла».</li>
          <li>Правый клик по значку → «Подключиться».{(" Введите пароль из письма." if password else "")}</li>
        </ol>
        {pwd_block}"""
    else:
        steps = f"""
        <ol style="font-size:14px;color:#334155;line-height:1.8;padding-left:20px">
          <li>Установите приложение клиента VPN для вашего устройства.</li>
          <li>Импортируйте вложенный файл конфигурации.</li>
          <li>Нажмите «Подключиться».{(" Логин и пароль — в этом письме." if password else "")}</li>
        </ol>
        {pwd_block}"""
    return steps


def send_client_email(
    smtp_host: str, smtp_port: int, smtp_user: str | None, smtp_password: str | None,
    smtp_from: str, smtp_tls: bool, to_email: str, client_name: str,
    server_name: str = "VPN", kind: str = "openvpn",
    attachments: list | None = None,      # [(bytes, maintype, subtype, filename)]
    password: str | None = None,
    archive_password: str | None = None,
    include_instructions: bool = True,
) -> None:
    """Письмо клиенту с вложениями (.ovpn/.exe), паролем и инструкцией."""
    msg = EmailMessage()
    msg["Subject"] = f"Доступ к {server_name} — настройка VPN"
    msg["From"] = smtp_from
    msg["To"] = to_email

    # plain-text fallback
    plain = f"Здравствуйте, {client_name}!\n\nВо вложении файлы для подключения к VPN «{server_name}»."
    if archive_password:
        plain += f"\n\nПароль от архива (.zip): {archive_password}"
    if password:
        plain += f"\n\nПароль для подключения к VPN: {password}"
    plain += "\n\nИнструкция по настройке — в HTML-версии письма."
    msg.set_content(plain)

    has_installer = any(a[3].endswith((".exe", ".zip")) for a in (attachments or []))
    instr = _instructions_html(kind, has_installer, password, archive_password) \
        if include_instructions else ""
    html_body = f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:600px;margin:0 auto;color:#1e293b">
      <div style="background:linear-gradient(135deg,#6366f1,#4f46e5);padding:24px;border-radius:14px 14px 0 0">
        <h1 style="color:#fff;margin:0;font-size:20px">Click VPN — доступ настроен</h1>
      </div>
      <div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 14px 14px;padding:24px">
        <p style="font-size:15px">Здравствуйте, <b>{_html.escape(client_name)}</b>!</p>
        <p style="font-size:14px;color:#475569">Во вложении — всё необходимое для подключения к корпоративному VPN «{_html.escape(server_name)}».</p>
        {("<h3 style='font-size:15px;margin-top:20px'>Как подключиться</h3>" + instr) if instr else ""}
        <p style="font-size:12px;color:#94a3b8;margin-top:22px;border-top:1px solid #f1f5f9;padding-top:14px">
          Файлы во вложении содержат ваш персональный доступ — не передавайте их другим.<br>
          С уважением, служба поддержки {_html.escape(server_name)}.
        </p>
      </div>
    </div>"""
    msg.add_alternative(html_body, subtype="html")

    for data, maintype, subtype, filename in (attachments or []):
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    _send(msg, smtp_host, smtp_port, smtp_user, smtp_password, smtp_tls)


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
