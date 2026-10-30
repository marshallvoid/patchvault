#!/usr/bin/env python3
"""Build PatchVault for the current OS into dist/<package>/ and dist/<package>.zip."""

import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import patchvault  # noqa: E402

NAME = "PatchVault"
DIST = ROOT / "dist"
WORK = DIST / "work"
DATA_FOLDERS = ("downloaded", "covers")


def package_name():
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    return f"{NAME}-{system}-{'x86_64' if machine == 'amd64' else machine}"


def write_build_config():
    """Embed the configuration: users of a packaged build have no .env file."""
    config = patchvault.load_config()
    missing = [key for key in patchvault.CONFIG_KEYS if not config.get(key)]
    if missing:
        sys.exit(f"Missing configuration: {', '.join(missing)} (see .env.example)")
    patchvault.parse_public_key(config["PATCHVAULT_PUBLIC_KEY"])
    values = {key: config[key] for key in patchvault.CONFIG_KEYS}
    generated = WORK / "generated"
    generated.mkdir(parents=True)
    (generated / "patchvault_build_config.py").write_text(f"VALUES = {values!r}\n", encoding="utf-8")
    return generated


def compile_app(generated):
    args = ["--noconfirm", "--log-level", "WARN", "--windowed", "--name", NAME,
            "--paths", str(generated), "--hidden-import", "patchvault_build_config",
            "--hidden-import", "PIL._tkinter_finder", "--collect-data", "certifi",
            "--distpath", str(WORK / "output"), "--workpath", str(WORK / "pyinstaller"),
            "--specpath", str(WORK)]
    # macOS needs a folder-based .app; a one-file binary inside an .app is deprecated.
    if sys.platform != "darwin":
        args.append("--onefile")
    PyInstaller.__main__.run([*args, str(ROOT / "patchvault.py")])


def stage(package):
    target = DIST / package
    target.mkdir()
    output = WORK / "output"
    if sys.platform == "darwin":
        subprocess.run(["ditto", str(output / f"{NAME}.app"), str(target / f"{NAME}.app")], check=True)
    else:
        binary = f"{NAME}.exe" if sys.platform == "win32" else NAME
        shutil.move(str(output / binary), str(target / binary))
    for folder in DATA_FOLDERS:
        (target / folder).mkdir()
    return target


def archive(source):
    target = source.with_suffix(".zip")
    if sys.platform == "darwin":
        # ditto keeps the symlinks and signature inside the .app.
        subprocess.run(["ditto", "-c", "-k", str(source), str(target)], check=True)
        return target
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(source.rglob("*")):
            output.write(path, path.relative_to(source))
    return target


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    compile_app(write_build_config())
    target = archive(stage(package_name()))
    shutil.rmtree(WORK)
    print(target)


if __name__ == "__main__":
    main()
