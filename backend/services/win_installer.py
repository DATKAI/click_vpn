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
BUNDLE_AMD64 = os.path.join(ASSETS_DIR, "openvpn-installer-amd64.msi")
BUNDLE_X86 = os.path.join(ASSETS_DIR, "openvpn-installer-x86.msi")
# Совместимость со старой установкой (одиночный amd64-бандл)
BUNDLE_LEGACY = os.path.join(ASSETS_DIR, "openvpn-installer.msi")


def _amd64_path() -> str | None:
    for p in (BUNDLE_AMD64, BUNDLE_LEGACY):
        if os.path.exists(p):
            return p
    return None


def is_available() -> tuple[bool, str]:
    """Готов ли сервер собирать установщики."""
    if shutil.which("makensis") is None:
        return False, "NSIS не установлен. Запустите: bash /opt/click-vpn/install-client-installer.sh"
    if not _amd64_path():
        return False, "Нет бандла OpenVPN. Запустите: bash /opt/click-vpn/install-client-installer.sh"
    return True, "ok"


def _safe_name(name: str) -> str:
    """Имя файла без спецсимволов для NSIS/Windows."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "client"


_NSI_HEAD = r"""
!include "MUI2.nsh"
!include "x64.nsh"
!include "LogicLib.nsh"

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

Var OVPN_DIR

Section "Install"
    SetOutPath "$TEMP"

    ; Каталог OpenVPN по разрядности системы
    ${{If}} ${{RunningX64}}
        StrCpy $OVPN_DIR "$PROGRAMFILES64\OpenVPN"
    ${{Else}}
        StrCpy $OVPN_DIR "$PROGRAMFILES32\OpenVPN"
    ${{EndIf}}

    ; --- Установка OpenVPN, если не установлен ---
    IfFileExists "$OVPN_DIR\bin\openvpn-gui.exe" openvpn_present install_openvpn

    install_openvpn:
        DetailPrint "Установка OpenVPN..."
{install_block}
        ExecWait 'msiexec /i "$TEMP\ovpn-setup.msi" /qn /norestart' $0
        Delete "$TEMP\ovpn-setup.msi"
        Sleep 2000

    openvpn_present:
        CreateDirectory "$OVPN_DIR\config"
        SetOutPath "$OVPN_DIR\config"
        File "/oname={profile_name}.ovpn" "{ovpn_path}"
        CreateShortcut "$DESKTOP\OpenVPN GUI.lnk" "$OVPN_DIR\bin\openvpn-gui.exe"

        DetailPrint "Профиль {display_name} установлен."
        MessageBox MB_OK "Готово! VPN-профиль установлен.$\r$\n$\r$\nЗапустите 'OpenVPN GUI' с рабочего стола, нажмите правой кнопкой на значок OpenVPN в трее (справа от часов) и выберите 'Подключиться'."
SectionEnd
"""

# Блок установки для универсального (32+64) и только-amd64 случая
_INSTALL_DUAL = r"""        ${{If}} ${{RunningX64}}
            File "/oname=$TEMP\ovpn-setup.msi" "{amd64}"
        ${{Else}}
            File "/oname=$TEMP\ovpn-setup.msi" "{x86}"
        ${{EndIf}}"""

_INSTALL_AMD64 = r"""        File "/oname=$TEMP\ovpn-setup.msi" "{amd64}" """


def build_installer(client_name: str, ovpn_content: str) -> bytes:
    """Собирает .exe установщик для клиента. Бросает RuntimeError при ошибке."""
    ok, msg = is_available()
    if not ok:
        raise RuntimeError(msg)

    amd64 = _amd64_path()
    has_x86 = os.path.exists(BUNDLE_X86)

    safe = _safe_name(client_name)
    workdir = tempfile.mkdtemp(prefix="clickvpn-nsis-")
    try:
        ovpn_path = os.path.join(workdir, f"{safe}.ovpn")
        with open(ovpn_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(ovpn_content)

        if has_x86:
            install_block = _INSTALL_DUAL.format(amd64=amd64, x86=BUNDLE_X86)
        else:
            install_block = _INSTALL_AMD64.format(amd64=amd64)

        out_exe = os.path.join(workdir, f"ClickVPN-{safe}-setup.exe")
        nsi = _NSI_HEAD.format(
            display_name=client_name.replace('"', "'"),
            out_exe=out_exe,
            ovpn_path=ovpn_path,
            profile_name=safe,
            install_block=install_block,
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
