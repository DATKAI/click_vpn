"""Генерация персонального Windows-установщика (.exe) для OpenVPN-клиента.

Собирает через NSIS (makensis) самораспаковывающийся .exe, который:
  1. Запрашивает права администратора (UAC).
  2. Тихо ставит OpenVPN Community GUI (если ещё не установлен).
  3. Кладёт персональный .ovpn профиль в config OpenVPN.
  4. Создаёт ярлык OpenVPN GUI на рабочем столе.

Требует на сервере:
  - makensis (apt install nsis)
  - бандл OpenVPN-установщика в  $DATA_DIR/assets/openvpn-installer.exe
Оба ставит  install-client-installer.sh.
"""
import os
import re
import shutil
import tempfile
import subprocess

DATA_DIR = os.getenv("DATA_DIR", "./data")
ASSETS_DIR = os.path.join(DATA_DIR, "assets")
OPENVPN_BUNDLE = os.path.join(ASSETS_DIR, "openvpn-installer.msi")


def is_available() -> tuple[bool, str]:
    """Готов ли сервер собирать установщики."""
    if shutil.which("makensis") is None:
        return False, "NSIS не установлен. Запустите: bash /opt/click-vpn/install-client-installer.sh"
    if not os.path.exists(OPENVPN_BUNDLE):
        return False, "Нет бандла OpenVPN. Запустите: bash /opt/click-vpn/install-client-installer.sh"
    return True, "ok"


def _safe_name(name: str) -> str:
    """Имя файла без спецсимволов для NSIS/Windows."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "client"


_NSI_TEMPLATE = r"""
!include "MUI2.nsh"

Name "Click VPN — {display_name}"
OutFile "{out_exe}"
RequestExecutionLevel admin
InstallDir "$PROGRAMFILES64\OpenVPN"
ShowInstDetails show
Unicode true

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"
!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SetOutPath "$TEMP"

    ; --- Установка OpenVPN, если не установлен ---
    IfFileExists "$PROGRAMFILES64\OpenVPN\bin\openvpn-gui.exe" openvpn_present install_openvpn

    install_openvpn:
        DetailPrint "Установка OpenVPN..."
        File "/oname=$TEMP\ovpn-setup.msi" "{bundle_path}"
        ExecWait 'msiexec /i "$TEMP\ovpn-setup.msi" /qn /norestart' $0
        Delete "$TEMP\ovpn-setup.msi"
        Sleep 2000

    openvpn_present:
        ; --- Каталог config (для всех пользователей) ---
        CreateDirectory "$PROGRAMFILES64\OpenVPN\config"
        SetOutPath "$PROGRAMFILES64\OpenVPN\config"
        File "/oname={profile_name}.ovpn" "{ovpn_path}"

        ; --- Ярлык GUI на рабочем столе ---
        CreateShortcut "$DESKTOP\OpenVPN GUI.lnk" "$PROGRAMFILES64\OpenVPN\bin\openvpn-gui.exe"

        DetailPrint "Профиль {display_name} установлен."
        MessageBox MB_OK "Готово! VPN-профиль установлен.$\r$\n$\r$\nЗапустите 'OpenVPN GUI' с рабочего стола, нажмите правой кнопкой на значок OpenVPN в трее (справа от часов) и выберите 'Подключиться'."
SectionEnd
"""


def build_installer(client_name: str, ovpn_content: str) -> bytes:
    """Собирает .exe установщик для клиента. Бросает RuntimeError при ошибке."""
    ok, msg = is_available()
    if not ok:
        raise RuntimeError(msg)

    safe = _safe_name(client_name)
    workdir = tempfile.mkdtemp(prefix="clickvpn-nsis-")
    try:
        ovpn_path = os.path.join(workdir, f"{safe}.ovpn")
        with open(ovpn_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(ovpn_content)

        out_exe = os.path.join(workdir, f"ClickVPN-{safe}-setup.exe")
        nsi = _NSI_TEMPLATE.format(
            display_name=client_name.replace('"', "'"),
            out_exe=out_exe,
            bundle_path=OPENVPN_BUNDLE,
            ovpn_path=ovpn_path,
            profile_name=safe,
        )
        nsi_path = os.path.join(workdir, "installer.nsi")
        with open(nsi_path, "w", encoding="utf-8") as f:
            f.write(nsi)

        proc = subprocess.run(
            ["makensis", "-V2", nsi_path],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not os.path.exists(out_exe):
            raise RuntimeError(
                "Ошибка сборки установщика: " + (proc.stderr or proc.stdout or "makensis failed")
            )
        with open(out_exe, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
