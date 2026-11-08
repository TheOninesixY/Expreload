import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(ROOT, "expreload.py")
ICON = os.path.join(ROOT, "expreload.ico")
VERSION_FILE = os.path.join(ROOT, "VERSION.md")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "tmp", "build")
CONTENTS_DIR = "lib"


def read_version():
    if not os.path.exists(VERSION_FILE):
        raise SystemExit("VERSION.md not found")
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                return line.strip()
    raise SystemExit("VERSION.md is empty")


def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found, installing...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )


def clean_previous():
    for path in (DIST, BUILD):
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
        except PermissionError:
            raise SystemExit(
                f"cannot clean {path}: a running Expreload.exe may still hold it. "
                "Close the app (tray -> 退出) and run build again."
            )


def make_zip(target_dir, version, arc_root="Expreload"):
    safe = re.sub(r'[<>:"/\\|?*]', "_", version)
    zip_path = os.path.join(DIST, f"Expreload_{safe}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, _, files in os.walk(target_dir):
            for name in files:
                full = os.path.join(folder, name)
                rel = os.path.relpath(full, target_dir)
                zf.write(full, os.path.join(arc_root, rel))
    return zip_path


def build_exe():
    version = read_version()
    print(f"Building {version} (folder) ...")

    clean_previous()

    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        "Expreload",
        "--distpath",
        DIST,
        "--workpath",
        BUILD,
        "--specpath",
        BUILD,
        "--contents-directory",
        CONTENTS_DIR,
    ]

    if os.path.exists(ICON):
        args += ["--icon", ICON]

    args += ["--add-data", f"{VERSION_FILE};."]
    if os.path.exists(ICON):
        args += ["--add-data", f"{ICON};."]

    args.append(ENTRY)

    result = subprocess.run(args)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with code {result.returncode}")

    produced_dir = os.path.join(DIST, "Expreload")
    produced_exe = os.path.join(produced_dir, "Expreload.exe")
    if not os.path.exists(produced_exe):
        raise SystemExit("Expreload.exe was not produced")

    safe = re.sub(r'[<>:"/\\|?*]', "_", version)
    target_dir = os.path.join(DIST, f"Expreload_{safe}")
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.replace(produced_dir, target_dir)

    target_exe = os.path.join(target_dir, "Expreload.exe")
    size_mb = round(os.path.getsize(target_exe) / (1024 * 1024), 2)
    print(f"Built: {target_exe} ({size_mb} MB, folder mode)")

    zip_path = make_zip(target_dir, version)
    zip_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
    print(f"Archive: {zip_path} ({zip_mb} MB)")
    print(f"Distribute the whole folder: {target_dir}")
    return target_exe


def main():
    ensure_pyinstaller()
    build_exe()


if __name__ == "__main__":
    main()