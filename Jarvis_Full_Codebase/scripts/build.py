"""Build Jarvis into a standalone .exe and register it for Windows auto-start.

Usage:
    python scripts/build.py           # build only
    python scripts/build.py --install # build + create desktop shortcut + startup entry
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
EXE_PATH = DIST_DIR / "Jarvis.exe"
ICON_PATH = PROJECT_ROOT / "jarvis.ico"


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[BUILD] Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build():
    ensure_pyinstaller()
    spec = PROJECT_ROOT / "jarvis.spec"
    if not spec.exists():
        print(f"[BUILD] Spec not found: {spec}")
        sys.exit(1)

    print("[BUILD] Starting PyInstaller build...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(spec), "--noconfirm"],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        print("[BUILD] PyInstaller failed.")
        sys.exit(1)

    if EXE_PATH.exists():
        print(f"[BUILD] ✅ Build complete: {EXE_PATH}")
        print(f"[BUILD]    Size: {EXE_PATH.stat().st_size / (1024*1024):.1f} MB")
    else:
        print("[BUILD] ❌ exe not found after build.")
        sys.exit(1)


def create_desktop_shortcut():
    """Create a desktop shortcut to Jarvis.exe."""
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
    if not desktop.exists():
        print("[BUILD] Desktop folder not found, skipping shortcut.")
        return

    shortcut_path = desktop / "Jarvis.lnk"
    ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{EXE_PATH}"
$Shortcut.WorkingDirectory = "{DIST_DIR}"
$Shortcut.IconLocation = "{ICON_PATH}"
$Shortcut.Description = "Jarvis Desktop AI Assistant"
$Shortcut.Save()
'''
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        check=True,
    )
    print(f"[BUILD] ✅ Desktop shortcut: {shortcut_path}")


def create_startup_entry():
    """Place a shortcut in the Windows Startup folder."""
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    shortcut_path = startup / "Jarvis.lnk"

    ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{EXE_PATH}"
$Shortcut.Arguments = "--autostart"
$Shortcut.WorkingDirectory = "{DIST_DIR}"
$Shortcut.IconLocation = "{ICON_PATH}"
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Jarvis Desktop AI (Auto-start)"
$Shortcut.Save()
'''
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        check=True,
    )
    print(f"[BUILD] ✅ Startup entry: {shortcut_path}")


def main():
    install = "--install" in sys.argv

    # Ensure icon exists
    if not ICON_PATH.exists():
        print("[BUILD] Generating icon...")
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_ico.py")],
            check=True,
        )

    build()

    if install:
        create_desktop_shortcut()
        create_startup_entry()
        print("\n[BUILD] 🎉 Jarvis installed!")
        print("   → Desktop shortcut created")
        print("   → Will auto-start on next login")
        print(f"   → You can also run: {EXE_PATH}")
    else:
        print("\n[BUILD] To install shortcuts, run: python scripts/build.py --install")


if __name__ == "__main__":
    main()
