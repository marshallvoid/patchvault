import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from .config import PROGRAM_NAME


def asset_name():
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    return f"{PROGRAM_NAME}-{system}-{'x86_64' if machine == 'amd64' else machine}.zip"


def program_path():
    executable = Path(sys.executable).resolve()
    return executable.parents[2] if sys.platform == "darwin" else executable


def can_self_update(update):
    return bool(getattr(sys, "frozen", False) and update.get("asset") and os.access(program_path().parent, os.W_OK))


def backup_path(program):
    return program.with_name(program.name + ".old")


def remove_path(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def remove_old_program():
    # A program cannot delete itself while running, so the next start removes the backup.
    if getattr(sys, "frozen", False):
        try:
            remove_path(backup_path(program_path()))
        except OSError:
            pass


def extract_program(archive, target, name):
    if sys.platform == "darwin":
        # ditto keeps the symlinks and signature inside the .app.
        subprocess.run(["ditto", "-x", "-k", str(archive), str(target)], check=True)
    else:
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                parts = info.filename.split("/")
                if parts[0] == name and ".." not in parts:
                    source.extract(info, target)
    extracted = target / name
    if not extracted.exists():
        raise RuntimeError(f"bản cập nhật không có {name}")
    if sys.platform != "win32" and extracted.is_file():
        extracted.chmod(0o755)
    return extracted


def swap_program(new, program):
    # Every OS allows moving a running program aside, but not overwriting it.
    backup = backup_path(program)
    remove_path(backup)
    os.replace(program, backup)
    try:
        shutil.move(str(new), str(program))
    except OSError:
        os.replace(backup, program)
        raise


def restart_program():
    program = program_path()
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-n", str(program)])
        return
    # The new process must unpack its own bundle, not inherit this one's.
    subprocess.Popen([str(program)], env={**system_env(), "PYINSTALLER_RESET_ENVIRONMENT": "1"})


def open_in_system(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(
            ["xdg-open", str(path)], env=system_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def system_env():
    # Desktop tools must not inherit the bundled library path of a packaged build.
    env = os.environ.copy()
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env
