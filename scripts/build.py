#!/usr/bin/env python3

import os
import re
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from patchvault import config as settings
from patchvault import system

NAME = settings.PROGRAM_NAME
DIST = ROOT / "dist"
WORK = DIST / "work"
DATA_FOLDERS = ("downloaded", "covers")
BUNDLED_PACKAGES = ("pillow", "certifi", "pyinstaller")
LICENSE_FILE = re.compile(r"licen[cs]e|copying|notice", re.IGNORECASE)


def write_build_config():
    config = settings.load_config()
    missing = [key for key in settings.CONFIG_KEYS if not config.get(key)]
    if missing:
        sys.exit(f"Missing configuration: {', '.join(missing)} (see .env.example)")
    values = {key: config[key] for key in settings.CONFIG_KEYS}
    values[settings.VERSION_KEY] = os.environ.get(settings.VERSION_KEY, "")
    generated = WORK / "generated"
    generated.mkdir(parents=True)
    (generated / "patchvault_build_config.py").write_text(f"VALUES = {values!r}\n", encoding="utf-8")
    return generated


def compile_app(generated):
    args = [
        "--noconfirm",
        "--log-level",
        "WARN",
        "--windowed",
        "--name",
        NAME,
        "--paths",
        str(generated),
        "--hidden-import",
        "patchvault_build_config",
        "--hidden-import",
        "PIL._tkinter_finder",
        "--collect-data",
        "certifi",
        "--distpath",
        str(WORK / "output"),
        "--workpath",
        str(WORK / "pyinstaller"),
        "--specpath",
        str(WORK),
    ]
    # macOS needs a folder-based .app; a one-file binary inside an .app is deprecated.
    if sys.platform != "darwin":
        args.append("--onefile")
    PyInstaller.__main__.run([*args, str(ROOT / "main.py")])


def read_python_license():
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    prefix = Path(sys.base_prefix)
    return first_text([prefix / "lib" / version / "LICENSE.txt", prefix / "LICENSE.txt", prefix / "LICENSE"])


def first_text(paths):
    return next((path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file()), None)


def read_package_licenses(name):
    distribution = metadata.distribution(name)
    files = [
        file for file in distribution.files or [] if ".dist-info" in str(file) and LICENSE_FILE.search(Path(file).name)
    ]
    texts = [Path(str(distribution.locate_file(file))).read_text(encoding="utf-8", errors="replace") for file in files]
    return "\n\n".join(texts) or None


def third_party_licenses():
    sections = [
        ("Python", read_python_license(), "https://docs.python.org/3/license.html"),
        (
            "Tcl/Tk",
            first_text([ROOT / "licenses" / "tcl-tk.txt"]),
            "https://www.tcl-lang.org/software/tcltk/license.html",
        ),
    ]
    sections += [(name, read_package_licenses(name), f"https://pypi.org/project/{name}/") for name in BUNDLED_PACKAGES]
    parts = ["PatchVault's packaged builds include the software below. Its own license is in LICENSE."]
    for name, text, url in sections:
        if text is None:
            print(f"warning: license text of {name} not found, linking to it instead")
        parts.append(f"{'=' * 78}\n{name}\n{'=' * 78}\n\n{text or f'See {url}'}")
    return "\n\n".join(parts) + "\n"


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
    shutil.copy(ROOT / "LICENSE", target / "LICENSE")
    (target / "THIRD_PARTY_LICENSES.txt").write_text(third_party_licenses(), encoding="utf-8")
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
    target = archive(stage(Path(system.asset_name()).stem))
    shutil.rmtree(WORK)
    print(target)


if __name__ == "__main__":
    main()
