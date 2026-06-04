"""Проверка истечения сертификатов и email-напоминания.

Пороги уведомлений: 30, 7, 1 день. Каждый порог уведомляется один раз
(антидубль через VPNUser.expiry_notified). Перевыпуск сбрасывает счётчик.
"""
import time
import threading
from datetime import datetime

THRESHOLDS = [30, 7, 1]   # по убыванию


def _target_threshold(days_left: float):
    """Возвращает порог для текущего остатка дней, либо None."""
    if days_left <= 1:
        return 1
    if days_left <= 7:
        return 7
    if days_left <= 30:
        return 30
    return None


def check_and_notify(SessionLocal, VPNUser, Settings):
    """Один проход: шлёт напоминания, обновляет expiry_notified."""
    db = SessionLocal()
    try:
        s = db.query(Settings).filter(Settings.id == 1).first()
        if not s or not s.expiry_notify_enabled:
            return
        if not s.smtp_host or not s.smtp_from:
            return
        from services import mailer
        now = datetime.utcnow()
        users = (
            db.query(VPNUser)
            .filter(VPNUser.archived == False,
                    VPNUser.cert_expires_at.isnot(None),
                    VPNUser.email.isnot(None))
            .all()
        )
        for u in users:
            if not u.email:
                continue
            days_left = (u.cert_expires_at - now).total_seconds() / 86400
            if days_left < 0:
                continue  # уже истёк — отдельная история
            thr = _target_threshold(days_left)
            if thr is None:
                continue
            prev = u.expiry_notified or 0
            # уведомляем, если ещё не слали на этом (или меньшем) пороге
            if prev != 0 and thr >= prev:
                continue
            try:
                _send_reminder(s, u, int(days_left))
                u.expiry_notified = thr
                db.commit()
            except Exception:
                db.rollback()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _send_reminder(s, user, days_left: int):
    import html as _html
    when = user.cert_expires_at.strftime("%d.%m.%Y") if user.cert_expires_at else "—"
    html_body = f"""
    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#1e293b">
      <div style="background:#f59e0b;padding:20px;border-radius:14px 14px 0 0">
        <h1 style="color:#fff;margin:0;font-size:18px">Срок доступа к VPN истекает</h1>
      </div>
      <div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 14px 14px;padding:22px">
        <p style="font-size:15px">Здравствуйте, <b>{_html.escape(user.full_name or user.username)}</b>!</p>
        <p style="font-size:14px;color:#475569">
          Ваш сертификат доступа к корпоративному VPN «{_html.escape(s.server_name or 'VPN')}»
          истекает <b>через {days_left} дн.</b> ({when}).
        </p>
        <p style="font-size:14px;color:#475569">
          После этой даты подключение к VPN перестанет работать. Пожалуйста,
          заранее обратитесь к администратору для продления доступа.
        </p>
        <p style="font-size:12px;color:#94a3b8;margin-top:18px;border-top:1px solid #f1f5f9;padding-top:12px">
          Это автоматическое уведомление. Служба поддержки {_html.escape(s.server_name or 'VPN')}.
        </p>
      </div>
    </div>"""
    _send_raw(s, user.email, f"VPN: доступ истекает через {days_left} дн.", html_body)


def _send_raw(s, to_email, subject, html_body):
    """Прямая отправка HTML-письма (минуя шаблон клиента)."""
    import smtplib, ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_from
    msg["To"] = to_email
    msg.set_content("Срок доступа к VPN истекает. Обратитесь к администратору для продления.")
    msg.add_alternative(html_body, subtype="html")
    port = s.smtp_port or 587
    tls = s.smtp_tls if s.smtp_tls is not None else True
    if tls and port == 465:
        with smtplib.SMTP_SSL(s.smtp_host, port, context=ssl.create_default_context(), timeout=20) as srv:
            if s.smtp_user:
                srv.login(s.smtp_user, s.smtp_password or "")
            srv.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, port, timeout=20) as srv:
            if tls:
                srv.starttls(context=ssl.create_default_context())
            if s.smtp_user:
                srv.login(s.smtp_user, s.smtp_password or "")
            srv.send_message(msg)


def start_checker(SessionLocal, VPNUser, Settings):
    """Фоновый поток: проверка раз в 12 часов."""
    def loop():
        time.sleep(60)  # дать приложению подняться
        while True:
            try:
                check_and_notify(SessionLocal, VPNUser, Settings)
            except Exception:
                pass
            time.sleep(12 * 3600)
    threading.Thread(target=loop, daemon=True).start()
