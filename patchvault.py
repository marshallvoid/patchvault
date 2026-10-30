#!/usr/bin/env python3
"""PatchVault: download, track and repackage Vietnamese patches for Switch games.

Needs Python 3.8+ with Tkinter; Pillow is optional and enables artwork. Catalogs are added
from the app window, and each one keeps its own downloads, artwork and state.

Every patch is repackaged as
    downloaded/<catalog>/[VI] - <Game name>/[<game version>-unreviewed] [VI].zip
with a contents/<title id>/... layout (Deflate, level 9).

    python3 patchvault.py            open the app window
    python3 patchvault.py --check    print available updates to the terminal
"""

import argparse
import hashlib
import http.client
import json
import os
import platform
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tkinter as tk
    from tkinter import font as tkfont, messagebox, ttk
except ImportError:
    tk = None

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except ImportError:
    Image = ImageDraw = ImageFilter = ImageOps = ImageTk = None

GDRIVE_URL = "https://drive.usercontent.google.com/download?export=download&confirm=t&id="
USER_AGENT = "PatchVault/1.0"
CHECK_INTERVAL = 30 * 60
CHECK_RETRY = 5 * 60
WORKERS = 2
ART_WORKERS = 4
RETRIES = 3
CHUNK = 256 * 1024
MOD_TAG = "VI"
INTERNAL_VERSION = "unreviewed"
MISSING_VERSION = "miss"
MAX_UNPACKED = 32 * 1024 ** 3
MAX_IMAGE = 8 * 1024 * 1024
MAX_CATALOG = 32 * 1024 * 1024
CATALOGS_DIR = ".catalogs"
MAX_UPDATE = 300 * 1024 * 1024
PROGRAM_NAME = "PatchVault"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
ART_KINDS = ("cover", "background")
DOWNLOAD_DIR = "downloaded"
SKIPPED_OUTSIDE = (".txt", ".md", ".url", ".pdf", ".htm", ".html")
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

CONFIG_KEYS = ("PATCHVAULT_RELEASES_URL",)
VERSION_KEY = "PATCHVAULT_VERSION"

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_GDRIVE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
TITLE_ID = re.compile(r"^[0-9A-Fa-f]{16}$")
ASSET_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

DISPLAY_VERSION = re.compile(r"v?\d+(?:\.\d+)*")


def parse_dotenv(path):
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def load_config():
    """Build-time values, then .env, then the real environment: later ones win."""
    values = {}
    try:
        import patchvault_build_config
        values.update(patchvault_build_config.VALUES)
    except ImportError:
        pass
    if not getattr(sys, "frozen", False):
        values.update(parse_dotenv(Path(__file__).resolve().parent / ".env"))
    values.update({key: value for key, value in os.environ.items() if key in CONFIG_KEYS})
    return values


def https_or_empty(value):
    if isinstance(value, str) and value.startswith("https://"):
        return value
    return ""


def resolve_urls(raw):
    urls = [https_or_empty(raw.get("url"))]
    for mirror in raw.get("mirrors") or []:
        if isinstance(mirror, str) and mirror.startswith("gdrive:"):
            file_id = mirror[len("gdrive:"):]
            if SAFE_GDRIVE.match(file_id):
                urls.append(GDRIVE_URL + file_id)
        else:
            urls.append(https_or_empty(mirror))
    return [url for url in dict.fromkeys(urls) if url]


def normalize(raw):
    """Validate one catalog entry. The catalog is remote data, so nothing in it is trusted."""
    if not isinstance(raw, dict):
        return None
    entry_id = raw.get("id")
    sha = raw.get("sha256")
    size = raw.get("size")
    urls = resolve_urls(raw)
    if not (isinstance(entry_id, str) and SAFE_ID.match(entry_id)):
        return None
    if not (isinstance(sha, str) and SAFE_SHA.match(sha)):
        return None
    if not (isinstance(size, int) and size > 0) or not urls:
        return None
    return {
        "id": entry_id,
        "name": str(raw.get("name") or entry_id),
        "version": str(raw.get("mod_version") or ""),
        "game_version": str(raw.get("game_version") or ""),
        "title_id": str(raw.get("title_id") or ""),
        "tagline": str(raw.get("tagline") or ""),
        "developer": str(raw.get("developer") or ""),
        "release": str(raw.get("release") or ""),
        "translator": str(raw.get("translator") or ""),
        "tags": [str(tag) for tag in raw.get("tags") or []],
        "description": [str(line) for line in raw.get("description") or []],
        "highlights": [{"title": str(entry.get("title") or ""), "text": str(entry.get("text") or "")}
                       for entry in raw.get("highlights") or [] if isinstance(entry, dict)],
        "done": [str(line) for line in raw.get("done") or []],
        "kept": [str(line) for line in raw.get("kept") or []],
        "changelog": str(raw.get("changelog") or ""),
        "credit": str(raw.get("credit") or ""),
        "cover": https_or_empty(raw.get("cover")),
        "background": https_or_empty(raw.get("background")),
        "sha256": sha.lower(),
        "size": size,
        "urls": urls,
    }


def fetch_catalog(url):
    """Return the raw bytes and parsed items of the catalog at url."""
    separator = "&" if "?" in url else "?"
    with http_get(f"{url}{separator}t={int(time.time())}") as response:
        raw = response.read(MAX_CATALOG + 1)
    if len(raw) > MAX_CATALOG:
        raise ValueError("catalog quá lớn")
    return raw, parse_catalog(json.loads(raw))


def parse_catalog(data):
    if not isinstance(data, dict):
        raise ValueError("file không phải catalog")
    items = []
    seen = set()
    for raw in data.get("mods") or []:
        entry = normalize(raw)
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            items.append(entry)
    if not items:
        raise ValueError("catalog không có game hợp lệ")
    return items


def base_dir():
    """Packaged builds keep data next to the program, source runs in .data at the project root."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent / ".data"
    executable = Path(sys.executable).resolve()
    if sys.platform != "darwin":
        return executable.parent
    # Name.app/Contents/MacOS/Name: use the folder holding the .app. A quarantined app runs
    # from a read-only randomized path, so fall back to the user's Application Support.
    folder = executable.parents[3]
    if "AppTranslocation" in folder.parts or not os.access(folder, os.W_OK):
        return Path.home() / "Library" / "Application Support" / "PatchVault"
    return folder


def ssl_context():
    """Packaged builds carry their own CA bundle: system CA paths differ between distros."""
    if not getattr(sys, "frozen", False):
        return None
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


SSL_CONTEXT = ssl_context()


def http_get(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)


def asset_name():
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    return f"{PROGRAM_NAME}-{system}-{'x86_64' if machine == 'amd64' else machine}.zip"


def program_path():
    """The file or .app that an update replaces."""
    executable = Path(sys.executable).resolve()
    return executable.parents[2] if sys.platform == "darwin" else executable


def can_self_update(update):
    return bool(getattr(sys, "frozen", False) and update.get("asset")
                and os.access(program_path().parent, os.W_OK))


def backup_path(program):
    return program.with_name(program.name + ".old")


def remove_path(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def remove_old_program():
    """A program cannot delete itself while running, so the next start removes the backup."""
    if getattr(sys, "frozen", False):
        try:
            remove_path(backup_path(program_path()))
        except OSError:
            pass


def extract_program(archive, target, name):
    """Extract only the program from a release zip, leaving the data folders alone."""
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
    """Move the running program aside, which every OS allows, then put the new one in place."""
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
        subprocess.Popen(["xdg-open", str(path)], env=system_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def system_env():
    """Environment for desktop tools, without the bundled library path of a packaged build."""
    env = os.environ.copy()
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    elif getattr(sys, "frozen", False):
        env.pop("LD_LIBRARY_PATH", None)
    return env


def game_folder(name):
    cleaned = re.sub(r'[:/\\?*"<>|\x00-\x1f]+', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:120]
    return f"[{MOD_TAG}] - {cleaned}"


def art_slug(name):
    text = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    text = text.encode("ascii", "ignore").decode("ascii").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def art_file_name(item, kind):
    suffix = Path(urlsplit(item[kind]).path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".img"
    return f"{art_slug(item['name']) or item['id']}-{kind}{suffix}"


def app_version():
    return load_config().get(VERSION_KEY) or "dev"


def parse_version(text):
    """Return (major, minor, patch) from a tag like v1.2.3, or None for anything else."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text).strip())
    return tuple(int(part) for part in match.groups()) if match else None


def display_version(game_version):
    match = DISPLAY_VERSION.search(game_version)
    return match.group(0) if match else MISSING_VERSION


def target_path(entry):
    """Path of the final file, relative to the vault root."""
    version = display_version(entry["game_version"])
    return Path(game_folder(entry["name"])) / f"[{version}-{INTERNAL_VERSION}] [{MOD_TAG}].zip"


def plan_repack(infos, title_id):
    """Map source zip members to contents/<title id>/... names, in source order."""
    if not TITLE_ID.match(title_id):
        raise RuntimeError(f"title id không hợp lệ: {title_id!r}")
    plan = {}
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        parts = [part for part in name.split("/") if part]
        if ".." in parts or "." in parts or name.startswith("/"):
            raise RuntimeError(f"đường dẫn không an toàn trong zip: {name}")
        if parts[0] == "__MACOSX" or parts[-1].lower() in JUNK_NAMES:
            continue
        index = next((i for i, part in enumerate(parts) if TITLE_ID.match(part)), None)
        if index is None:
            if parts[-1].lower().endswith(SKIPPED_OUTSIDE):
                continue
            raise RuntimeError(f"file nằm ngoài thư mục title id: {name}")
        if parts[index].lower() != title_id.lower():
            raise RuntimeError(f"title id lạ trong zip: {parts[index]}")
        if index + 1 == len(parts):
            raise RuntimeError(f"cấu trúc lạ trong zip: {name}")
        arcname = "/".join(["contents", title_id, *parts[index + 1:]])
        if arcname.lower() in {key.lower() for key in plan}:
            raise RuntimeError(f"file trùng tên trong zip: {arcname}")
        plan[arcname] = info
        total += info.file_size
    if not plan:
        raise RuntimeError(f"không có file nào trong {title_id}")
    if total > MAX_UNPACKED:
        raise RuntimeError("zip giải nén ra quá lớn")
    return plan


def member_info(name, date_time, mode, compress_type):
    info = zipfile.ZipInfo(name, date_time)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = compress_type
    if compress_type == zipfile.ZIP_DEFLATED:
        info._compresslevel = 9
    return info


def repack(source, target, title_id, cancel=None):
    """Rewrite a mod zip into the vault layout. Members are streamed, never extracted to disk."""
    temp = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as original, \
                zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            plan = plan_repack(original.infolist(), title_id)
            written = set()
            for arcname, member in plan.items():
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                folders = arcname.split("/")[:-1]
                for depth in range(1, len(folders) + 1):
                    folder = "/".join(folders[:depth]) + "/"
                    if folder not in written:
                        written.add(folder)
                        output.writestr(member_info(folder, member.date_time, 0o40755, zipfile.ZIP_STORED), b"")
                info = member_info(arcname, member.date_time, 0o100644, zipfile.ZIP_DEFLATED)
                info.file_size = member.file_size
                with original.open(member) as reader, output.open(info, "w") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK)
            expected = {name: member.CRC for name, member in plan.items()}
        with zipfile.ZipFile(temp) as result:
            actual = {i.filename: i.CRC for i in result.infolist() if not i.is_dir()}
            if actual != expected or result.testzip() is not None:
                raise RuntimeError("zip đóng gói lại không khớp với zip gốc")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Cancelled(Exception):
    pass


class Vault:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.catalogs_dir = self.base / CATALOGS_DIR
        self.catalogs_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.catalogs_dir / "index.json"
        self.parts = self.base / ".parts"
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancels = {}
        self.queue = queue.Queue()
        self.checked_at = None
        self.check_error = None
        self.checking = False
        self.app_update = None
        self.app_update_job = None
        registry = self._load_json(self.registry_path, {})
        self.catalogs = registry.get("catalogs", [])
        active = next((c for c in self.catalogs if c["folder"] == registry.get("active")), None)
        self._use_catalog(active or (self.catalogs[0] if self.catalogs else None))

    def _use_catalog(self, catalog):
        """Point the vault at one catalog. Every catalog has its own files and state."""
        self.catalog = catalog
        folder = catalog["folder"] if catalog else "_none"
        self.root = self.base / DOWNLOAD_DIR / folder
        self.art_dir = self.base / "covers" / folder
        self.art_index = self.catalogs_dir / f"{folder}.covers.json"
        self.state_path = self.catalogs_dir / f"{folder}.state.json"
        self.cache_path = self.catalogs_dir / f"{folder}.json"
        self.state = self._load_json(self.state_path, {})
        self.items = []
        self.checked_at = None
        self.check_error = None
        try:
            self.items = parse_catalog(json.loads(self.cache_path.read_bytes()))
        except (OSError, ValueError):
            pass

    def _save_registry(self):
        active = self.catalog["folder"] if self.catalog else None
        self._write_json(self.registry_path, {"active": active, "catalogs": self.catalogs})

    def add_catalog(self, name, url):
        """Add a catalog after confirming that the URL really serves one, then switch to it."""
        url = url.strip()
        if not url.startswith("https://"):
            raise ValueError("URL phải bắt đầu bằng https://")
        with self.lock:
            if any(catalog["url"] == url for catalog in self.catalogs):
                raise ValueError("catalog này đã có trong danh sách")
        raw, _ = fetch_catalog(url)
        name = name.strip() or urlsplit(url).hostname or "catalog"
        with self.lock:
            if self.busy():
                raise ValueError("hãy đợi các file đang tải xong rồi thêm catalog")
            taken = {catalog["folder"] for catalog in self.catalogs}
            slug = art_slug(name) or "catalog"
            folder, number = slug, 2
            while folder in taken:
                folder, number = f"{slug}-{number}", number + 1
            catalog = {"name": name, "url": url, "folder": folder}
            self.catalogs.append(catalog)
            self._write_bytes(self.catalogs_dir / f"{folder}.json", raw)
            self._use_catalog(catalog)
            self.checked_at = time.time()
            self._save_registry()
        return catalog

    def select_catalog(self, folder):
        with self.lock:
            catalog = next(c for c in self.catalogs if c["folder"] == folder)
            if catalog is not self.catalog:
                self._use_catalog(catalog)
                self._save_registry()

    def remove_catalog(self, folder):
        """Drop a catalog from the list. Its downloads stay on disk."""
        with self.lock:
            self.catalogs = [catalog for catalog in self.catalogs if catalog["folder"] != folder]
            if self.catalog and self.catalog["folder"] == folder:
                self._use_catalog(self.catalogs[0] if self.catalogs else None)
            self._save_registry()

    @staticmethod
    def _load_json(path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_json(self, path, value):
        self._write_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))

    def _write_bytes(self, path, data):
        temp = path.with_suffix(".tmp")
        temp.write_bytes(data)
        os.replace(temp, path)

    def check(self):
        with self.lock:
            if self.checking or not self.catalog:
                return
            self.checking = True
            catalog = self.catalog
        try:
            raw, items = fetch_catalog(catalog["url"])
            with self.lock:
                # The user may have switched catalogs while this one was loading.
                if self.catalog is catalog:
                    self._write_bytes(self.cache_path, raw)
                    self.items = items
                    self.check_error = None
                    self.checked_at = time.time()
        except (OSError, ValueError, http.client.HTTPException) as error:
            with self.lock:
                if self.catalog is catalog:
                    self.check_error = str(error) or type(error).__name__
        finally:
            with self.lock:
                self.checking = False

    def check_app_update(self):
        """Compare this build with the latest release and return the outcome.

        Source runs have no version, so they report "unversioned".
        """
        config = load_config()
        current = parse_version(config.get(VERSION_KEY, ""))
        url = config.get("PATCHVAULT_RELEASES_URL")
        if current is None or not url:
            return "unversioned"
        try:
            with http_get(url, {"Accept": "application/vnd.github+json"}) as response:
                release = json.loads(response.read())
            latest = parse_version(release["tag_name"])
            page = https_or_empty(release.get("html_url"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
            return "error"
        if latest is None:
            return "error"
        if latest <= current:
            return "current"
        if not page:
            return "error"
        with self.lock:
            self.app_update = {"tag": release["tag_name"], "url": page, "asset": self.pick_asset(release)}
        return "available"

    @staticmethod
    def pick_asset(release):
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == asset_name():
                url = asset.get("browser_download_url")
                size = asset.get("size")
                digest = asset.get("digest")
                valid = isinstance(url, str) and url.startswith("https://github.com/") \
                    and isinstance(size, int) and 0 < size <= MAX_UPDATE \
                    and isinstance(digest, str) and ASSET_DIGEST.match(digest)
                return {"url": url, "size": size, "sha256": digest[len("sha256:"):]} if valid else None
        return None

    def set_update_job(self, **fields):
        with self.lock:
            self.app_update_job = {**(self.app_update_job or {}), **fields}

    def download_update(self, asset, path):
        digest = hashlib.sha256()
        done = 0
        with http_get(asset["url"]) as response, open(path, "wb") as handle:
            for block in iter(lambda: response.read(CHUNK), b""):
                done += len(block)
                if done > asset["size"]:
                    raise RuntimeError("file lớn hơn kích thước trong release")
                digest.update(block)
                handle.write(block)
                self.set_update_job(done=done)
        if done != asset["size"] or digest.hexdigest() != asset["sha256"]:
            raise RuntimeError("bản cập nhật không khớp SHA-256 của release")

    def install_app_update(self, update):
        asset = update["asset"]
        program = program_path()
        work = self.parts / "app-update"
        self.set_update_job(phase="downloading", done=0, total=asset["size"], message="")
        try:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            archive = work / "update.zip"
            self.download_update(asset, archive)
            self.set_update_job(phase="installing")
            swap_program(extract_program(archive, work / "extracted", program.name), program)
            self.set_update_job(phase="done")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError,
                http.client.HTTPException) as error:
            self.set_update_job(phase="error", message=str(error) or type(error).__name__)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def local_path(self, record):
        path = (self.root / record["file"]).resolve()
        return path if self.root in path.parents else None

    def status_of(self, entry):
        record = self.state.get(entry["id"])
        path = self.local_path(record) if record else None
        if path is None or not path.is_file():
            return "missing"
        return "ok" if record["sha256"] == entry["sha256"] else "update"

    def snapshot(self):
        with self.lock:
            items = []
            for entry in self.items:
                record = self.state.get(entry["id"]) or {}
                public = dict(entry)
                public["status"] = self.status_of(entry)
                public["local_version"] = record.get("version", "")
                public["job"] = self.jobs.get(entry["id"])
                items.append(public)
            return {
                "items": items,
                "checked_at": self.checked_at,
                "checking": self.checking,
                "error": self.check_error,
                "dir": str(self.root),
                "catalogs": list(self.catalogs),
                "active": self.catalog["folder"] if self.catalog else None,
                "app_update": self.app_update,
                "app_update_job": self.app_update_job,
            }

    def enqueue(self, ids):
        with self.lock:
            known = {entry["id"]: entry for entry in self.items}
            for entry_id in ids:
                entry = known.get(entry_id)
                if entry and entry_id not in self.jobs:
                    self.jobs[entry_id] = {"phase": "queued", "done": 0, "total": entry["size"], "speed": 0}
                    self.cancels[entry_id] = threading.Event()
                    self.queue.put(entry)

    def cancel(self, entry_id):
        with self.lock:
            event = self.cancels.get(entry_id)
        if event:
            event.set()

    def busy(self):
        with self.lock:
            return any(job["phase"] != "error" for job in self.jobs.values())

    def snapshot_jobs(self):
        with self.lock:
            return list(self.jobs.values())

    def _update_job(self, entry_id, **fields):
        with self.lock:
            self.jobs[entry_id].update(fields)

    def _fetch(self, entry, url, part, cancel):
        """Download url into part, resuming when the server supports ranges."""
        have = part.stat().st_size if part.exists() else 0
        if have > entry["size"]:
            part.unlink()
            have = 0
        if have == entry["size"]:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            response = http_get(url, headers)
        except urllib.error.HTTPError as error:
            if error.code == 416:
                part.unlink(missing_ok=True)
            raise
        with response:
            mode = "ab" if have and response.status == 206 else "wb"
            done = have if mode == "ab" else 0
            mark, mark_done = time.monotonic(), done
            with open(part, mode) as handle:
                while True:
                    if cancel.is_set():
                        raise Cancelled
                    block = response.read(CHUNK)
                    if not block:
                        break
                    done += len(block)
                    if done > entry["size"]:
                        raise ValueError("file lớn hơn kích thước trong catalog")
                    handle.write(block)
                    now = time.monotonic()
                    if now - mark >= 0.5:
                        self._update_job(entry["id"], phase="downloading", done=done,
                                         speed=(done - mark_done) / (now - mark))
                        mark, mark_done = now, done
                self._update_job(entry["id"], done=done)
            if done < entry["size"]:
                raise OSError("kết nối bị ngắt giữa chừng")

    def _download(self, entry, cancel):
        """Return the path of a verified copy of the original file."""
        self.parts.mkdir(exist_ok=True)
        part = self.parts / f"{entry['id']}.{entry['sha256'][:12]}.part"
        last_error = "không tải được"
        for url in entry["urls"]:
            for attempt in range(RETRIES):
                try:
                    self._fetch(entry, url, part, cancel)
                except Cancelled:
                    raise
                except urllib.error.HTTPError as error:
                    last_error = f"HTTP {error.code}"
                    if error.code in (403, 404, 410):
                        break
                except (OSError, ValueError, http.client.HTTPException) as error:
                    last_error = str(error) or type(error).__name__
                else:
                    self._update_job(entry["id"], phase="verifying", speed=0)
                    if sha256_of(part) == entry["sha256"]:
                        return part
                    last_error = "SHA-256 không khớp"
                    part.unlink(missing_ok=True)
                    break
                time.sleep(1 + attempt)
            if url != entry["urls"][-1]:
                part.unlink(missing_ok=True)
        raise RuntimeError(last_error)

    def _commit(self, entry, target):
        with self.lock:
            previous = self.state.get(entry["id"])
            old_path = self.local_path(previous) if previous else None
            self.state[entry["id"]] = {
                "file": target.relative_to(self.root).as_posix(),
                "sha256": entry["sha256"],
                "version": entry["version"],
                "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write_json(self.state_path, self.state)
            if old_path and old_path != target:
                old_path.unlink(missing_ok=True)
                if old_path.parent != self.root and not any(old_path.parent.iterdir()):
                    old_path.parent.rmdir()

    def worker(self):
        while True:
            entry = self.queue.get()
            entry_id = entry["id"]
            cancel = self.cancels[entry_id]
            target = self.root / target_path(entry)
            try:
                if cancel.is_set():
                    raise Cancelled
                self._update_job(entry_id, phase="downloading")
                part = self._download(entry, cancel)
                self._update_job(entry_id, phase="repacking", speed=0)
                repack(part, target, entry["title_id"], cancel)
                part.unlink()
                self._commit(entry, target)
                with self.lock:
                    del self.jobs[entry_id]
            except Cancelled:
                with self.lock:
                    del self.jobs[entry_id]
            except Exception as error:
                self._update_job(entry_id, phase="error", message=str(error), speed=0)
            with self.lock:
                self.cancels.pop(entry_id, None)

    def clear_error(self, entry_id):
        with self.lock:
            job = self.jobs.get(entry_id)
            if job and job["phase"] == "error":
                del self.jobs[entry_id]


def format_size(value):
    if value >= 1e9:
        return f"{value / 1e9:.2f} GB"
    if value >= 1e6:
        return f"{value / 1e6:.1f} MB"
    return f"{max(1, round(value / 1e3))} KB"


def status_text(item):
    job = item["job"]
    if job:
        if job["phase"] == "error":
            return "Lỗi"
        if job["phase"] == "downloading":
            percent = 100 * job["done"] / job["total"]
            speed = f" · {format_size(job['speed'])}/s" if job["speed"] else ""
            return f"{percent:.0f}%{speed}"
        return {"queued": "Đang chờ", "verifying": "Đang kiểm tra", "repacking": "Đang đóng gói"}[job["phase"]]
    if item["status"] == "update":
        return f"Cập nhật: {item['local_version'] or '?'} → {item['version'] or 'mới'}"
    return "Đã tải" if item["status"] == "ok" else "Chưa tải"


def row_tag(item):
    job = item["job"]
    if job:
        return "error" if job["phase"] == "error" else "active"
    return item["status"]


COLORS = {
    "bg": "#1e2125", "panel": "#2c3036", "field": "#17191c", "ink": "#ecebe7", "muted": "#a0a29f",
    "line": "#3a3f46", "accent": "#e8894f", "accent_hover": "#f0a06d", "accent_ink": "#1a1208",
    "ok": "#6cc592", "bad": "#f2867e", "miss": "#b79cf0", "select": "#3d3229",
}
LIST_COLUMNS = (("game", "Game", 0), ("tags", "Thể loại", 250), ("version", "Phiên bản việt hóa", 155),
                ("game_version", "Phiên bản game", 130), ("size", "Dung lượng", 100), ("status", "Trạng thái", 210))
ROW_COLORS = {"update": "accent", "active": "accent", "ok": "ok", "error": "bad", "missing": "ink"}


class GameRow:
    """One list row. A Treeview cannot draw tag chips or rows of different heights."""

    def __init__(self, app, item):
        self.app = app
        self.id = item["id"]
        self.values = None
        self.selected = None
        self.frame = tk.Frame(app.list_frame, bg=COLORS["field"])
        self.cells = {}
        for index, (name, _, width) in enumerate(LIST_COLUMNS):
            self.frame.columnconfigure(index, minsize=width)
            if name == "tags":
                self.tag_cell = tk.Frame(self.frame, bg=COLORS["field"], padx=8, pady=6)
                self.tag_cell.grid(row=0, column=index, sticky="w")
                self.add_chips(item["tags"], width - 16)
            else:
                label = tk.Label(self.frame, anchor="w", justify="left", padx=8, pady=8, bd=0,
                                 font=app.font, bg=COLORS["field"], fg=COLORS["ink"])
                label.grid(row=0, column=index, sticky="nsew")
                self.cells[name] = label
        self.bind(self.frame)

    def add_chips(self, tags, limit):
        line, used = None, 0
        for tag in tags:
            width = self.app.chip_font.measure(tag) + 16
            if line is None or used + width > limit:
                line, used = tk.Frame(self.tag_cell, bg=COLORS["field"]), 0
                line.pack(anchor="w", pady=1)
            tk.Label(line, text=tag, font=self.app.chip_font, bg=COLORS["panel"], fg=COLORS["ink"],
                     padx=6, pady=1, bd=0).pack(side="left", padx=(0, 4))
            used += width

    def bind(self, widget):
        widget.bind("<Button-1>", lambda _: self.app.select(self.id))
        widget.bind("<Double-1>", lambda _: self.app.act())
        widget.bind("<Button-4>", lambda _: self.app.scroll_list(-1))
        widget.bind("<Button-5>", lambda _: self.app.scroll_list(1))
        widget.bind("<MouseWheel>", lambda event: self.app.scroll_list(-1 if event.delta > 0 else 1))
        for child in widget.winfo_children():
            self.bind(child)

    def fit(self, game_width):
        self.frame.columnconfigure(0, minsize=game_width)
        self.cells["game"].configure(wraplength=game_width - 16)

    def paint(self, item, selected):
        version = display_version(item["game_version"])
        missing = version == MISSING_VERSION
        values = (item["name"], item["version"] or "-", "Miss" if missing else version,
                  format_size(item["size"]), status_text(item), row_tag(item))
        if values != self.values:
            self.values = values
            color = COLORS[ROW_COLORS[values[-1]]]
            for name, text in zip(("game", "version", "game_version", "size", "status"), values):
                self.cells[name].configure(text=text, fg=color)
            if missing:
                self.cells["game_version"].configure(fg=COLORS["miss"])
        if selected != self.selected:
            self.selected = selected
            background = COLORS["select"] if selected else COLORS["field"]
            self.frame.configure(bg=background)
            self.tag_cell.configure(bg=background)
            for line in self.tag_cell.winfo_children():
                line.configure(bg=background)
            for label in self.cells.values():
                label.configure(bg=background)


class App:
    ART_SIZE = (426, 200)
    KEY_WIDTH = 128

    def __init__(self, vault):
        self.vault = vault
        self.root = tk.Tk(className="PatchVault")
        self.root.title(f"PatchVault {app_version()}")
        self.root.geometry("1640x900")
        self.root.minsize(1320, 620)
        self.root.configure(bg=COLORS["bg"])
        self.items = {}
        self.shown_ids = []
        self.rows = {}
        self.row_signature = None
        self.selected = None
        self.game_width = 300
        self.detail_id = None
        self.detail_key = None
        self.art_id = None
        self.art_image = None
        self.art_cache = {}
        self.art_signature = None
        self.genres = []
        self.selected_genres = set()
        self.genre_panel = None
        self.genre_panel_anchor = None
        self.app_check_result = None
        self.prompted_tag = None
        self.catalog_folders = []
        self.manager = None
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        family = tkfont.Font(root=self.root, font="TkDefaultFont").actual("family")
        if "Noto Sans" in tkfont.families(self.root):
            family = "Noto Sans"
        self.family = family
        self.font = (family, 10)
        self.bold = (family, 10, "bold")
        self.chip_font = tkfont.Font(root=self.root, family=family, size=9)
        c = COLORS
        style.configure(".", background=c["bg"], foreground=c["ink"], font=self.font,
                        bordercolor=c["line"], lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["field"])
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Title.TLabel", font=(family, 15, "bold"))
        style.configure("Name.TLabel", font=(family, 13, "bold"))
        style.configure("Error.TLabel", foreground=c["bad"])
        style.configure("TButton", background=c["panel"], foreground=c["ink"], padding=(10, 5), relief="flat")
        style.map("TButton", background=[("disabled", c["bg"]), ("active", c["line"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_ink"], font=self.bold)
        style.map("Accent.TButton", background=[("disabled", c["bg"]), ("active", c["accent_hover"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["ink"], insertcolor=c["ink"], padding=5)
        style.configure("TCombobox", fieldbackground=c["field"], background=c["panel"], foreground=c["ink"],
                        arrowcolor=c["muted"], padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["ink"])],
                  selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["ink"])])
        self.root.option_add("*TCombobox*Listbox.background", c["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["select"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["ink"])
        style.configure("TProgressbar", background=c["accent"], troughcolor=c["field"], borderwidth=0)
        style.configure("Vertical.TScrollbar", background=c["panel"], arrowcolor=c["muted"])

    def _build(self):
        header = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="PatchVault", style="Title.TLabel").pack(side="left")
        self.summary = ttk.Label(header, style="Muted.TLabel")
        self.summary.pack(side="left", padx=14)
        top = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        top.pack(fill="x")
        self.open_button = ttk.Button(top, text="Mở thư mục", command=self.open_folder)
        self.open_button.pack(side="right", padx=(6, 0))
        self.check_button = ttk.Button(top, text="Cập nhật catalog", command=self.check)
        self.check_button.pack(side="right", padx=(6, 0))
        self.app_update_button = ttk.Button(top, style="Accent.TButton", command=self.start_app_update)
        self.get_all_button = ttk.Button(top, command=self.get_all)
        self.get_all_button.pack(side="right", padx=(6, 0))
        self.update_all_button = ttk.Button(top, style="Accent.TButton", command=self.update_all)
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.rebuild_rows())
        ttk.Label(top, text="Catalog", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.catalog_box = ttk.Combobox(top, state="readonly", width=22)
        self.catalog_box.pack(side="left")
        self.catalog_box.bind("<<ComboboxSelected>>", lambda _: self.catalog_changed())
        self.manage_button = ttk.Button(top, text="Quản lý", command=self.open_manager)
        self.manage_button.pack(side="left", padx=(6, 16))
        ttk.Label(top, text="Tìm", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.search_entry = ttk.Entry(top, textvariable=self.search, width=26)
        self.search_entry.pack(side="left")
        self.genre_button = ttk.Button(top, text="Thể loại", command=self.toggle_genre_panel)
        self.genre_button.pack(side="left", padx=(8, 0))
        self.root.bind("<Button-1>", self.close_genre_panel_on_click, add="+")
        self.root.bind("<Configure>", self.close_genre_panel_on_move, add="+")

        body = ttk.Frame(self.root, padding=(14, 0, 14, 0))
        body.pack(fill="both", expand=True)
        table = tk.Frame(body, bg=COLORS["field"])
        table.pack(side="left", fill="both", expand=True)
        self.list_header = tk.Frame(table, bg=COLORS["panel"])
        self.list_header.pack(fill="x")
        for index, (_, title, width) in enumerate(LIST_COLUMNS):
            self.list_header.columnconfigure(index, minsize=width)
            tk.Label(self.list_header, text=title, anchor="w", padx=8, pady=7, bd=0, font=self.font,
                     bg=COLORS["panel"], fg=COLORS["muted"]).grid(row=0, column=index, sticky="ew")
        holder = tk.Frame(table, bg=COLORS["field"])
        holder.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(holder, bg=COLORS["field"], bd=0, highlightthickness=0, takefocus=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.list_canvas, bg=COLORS["line"])
        self.list_window = self.list_canvas.create_window(0, 0, anchor="nw", window=self.list_frame)
        self.list_frame.bind("<Configure>", lambda _: self.list_canvas.configure(
            scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind("<Configure>", self.fit_list)
        self.list_canvas.bind("<Button-4>", lambda _: self.scroll_list(-1))
        self.list_canvas.bind("<Button-5>", lambda _: self.scroll_list(1))
        self.list_canvas.bind("<MouseWheel>", lambda event: self.scroll_list(-1 if event.delta > 0 else 1))
        self.list_canvas.bind("<Up>", lambda _: self.move_selection(-1))
        self.list_canvas.bind("<Down>", lambda _: self.move_selection(1))
        self.list_canvas.bind("<Return>", lambda _: self.act())

        detail = ttk.Frame(body, width=440, padding=(14, 0, 0, 0))
        detail.pack(side="left", fill="y")
        detail.pack_propagate(False)
        detail.bind("<Configure>", self.fit_detail)
        self.art = tk.Label(detail, bg=COLORS["bg"], bd=0)
        self.name = ttk.Label(detail, style="Name.TLabel", justify="left")
        self.name.pack(fill="x", pady=(0, 2))
        self.state_label = ttk.Label(detail, justify="left", font=self.bold)
        self.state_label.pack(fill="x", pady=(2, 6))
        self.progress = ttk.Progressbar(detail, maximum=1000)
        self.action = ttk.Button(detail, command=self.act)
        self.action.pack(fill="x", pady=(0, 8))
        frame = ttk.Frame(detail)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", bd=0, highlightthickness=0, padx=0, pady=0, font=self.font,
                            bg=COLORS["bg"], fg=COLORS["ink"], state="disabled", cursor="arrow",
                            selectbackground=COLORS["select"], inactiveselectbackground=COLORS["select"])
        text_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._text_tags()

        foot = ttk.Frame(self.root, padding=(14, 8, 14, 10))
        foot.pack(fill="x")
        self.footer = ttk.Label(foot, style="Muted.TLabel")
        self.footer.pack(side="left")
        ttk.Label(foot, text=app_version(), style="Muted.TLabel").pack(side="right")
        self.app_check_button = ttk.Button(foot, text="Kiểm tra cập nhật app", command=self.check_app)
        self.app_check_button.pack(side="right", padx=(0, 10))

    def _text_tags(self):
        c = COLORS
        tag = self.text.tag_configure
        tag("tagline", font=(self.family, 11), spacing3=8)
        tag("chip", background=c["panel"], foreground=c["ink"], font=(self.family, 9))
        tag("chips", spacing3=4, spacing2=6)
        tag("heading", foreground=c["accent"], font=(self.family, 9, "bold"), spacing1=12, spacing3=4)
        tag("body", spacing2=2, spacing3=6)
        tag("row", lmargin2=self.KEY_WIDTH, tabs=(self.KEY_WIDTH,), spacing3=3)
        tag("key", foreground=c["muted"])
        tag("item", lmargin1=0, lmargin2=16, spacing2=2, spacing3=3)
        tag("strong", font=self.bold)
        tag("yes", foreground=c["ok"])
        tag("no", foreground=c["muted"])
        tag("missing_value", foreground=c["miss"])

    def fit_detail(self, event):
        for label in (self.name, self.state_label):
            label.configure(wraplength=event.width - 24)

    def toggle_genre_panel(self):
        if self.genre_panel:
            self.close_genre_panel()
            return
        panel = tk.Toplevel(self.root, bg=COLORS["line"], bd=0)
        panel.overrideredirect(True)
        panel.transient(self.root)
        panel.geometry(f"+{self.genre_button.winfo_rootx()}"
                       f"+{self.genre_button.winfo_rooty() + self.genre_button.winfo_height() + 4}")
        panel.bind("<Escape>", lambda _: self.close_genre_panel())
        self.genre_panel = panel
        self.genre_panel_anchor = self.root.geometry()
        body = tk.Frame(panel, bg=COLORS["panel"], padx=10, pady=8)
        body.pack(padx=1, pady=1)
        for index, genre in enumerate(self.genres):
            variable = tk.BooleanVar(value=genre in self.selected_genres)
            tk.Checkbutton(body, text=genre, variable=variable, anchor="w", bd=0, highlightthickness=0, font=self.font,
                           bg=COLORS["panel"], fg=COLORS["ink"], selectcolor=COLORS["field"],
                           activebackground=COLORS["line"], activeforeground=COLORS["ink"],
                           command=lambda g=genre, v=variable: self.toggle_genre(g, v.get())
                           ).grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 10), pady=1)
        ttk.Button(body, text="Bỏ chọn tất cả", command=self.clear_genres).grid(
            row=len(self.genres) // 2 + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        panel.focus_set()

    def close_genre_panel(self):
        if self.genre_panel:
            self.genre_panel.destroy()
            self.genre_panel = None

    def close_genre_panel_on_click(self, event):
        if self.genre_panel and event.widget.winfo_toplevel() is self.root and event.widget is not self.genre_button:
            self.close_genre_panel()

    def close_genre_panel_on_move(self, event):
        # The window also reports Configure when it stays put, so compare the real position.
        if self.genre_panel and event.widget is self.root and self.root.geometry() != self.genre_panel_anchor:
            self.close_genre_panel()

    def toggle_genre(self, genre, selected):
        if selected:
            self.selected_genres.add(genre)
        else:
            self.selected_genres.discard(genre)
        self.paint_genre_button()
        self.rebuild_rows()

    def clear_genres(self):
        self.selected_genres.clear()
        self.close_genre_panel()
        self.paint_genre_button()
        self.rebuild_rows()

    def paint_genre_button(self):
        selected = sorted(self.selected_genres, key=str.lower)
        text = "Thể loại" if not selected else f"Thể loại: {selected[0]}" if len(selected) == 1 \
            else f"Thể loại ({len(selected)})"
        self.genre_button.configure(text=text)

    def matches(self, item):
        query = self.search.get().strip().lower()
        haystack = " ".join([item["name"], item["developer"], item["translator"], *item["tags"]]).lower()
        return (not query or query in haystack) and \
            (not self.selected_genres or not self.selected_genres.isdisjoint(item["tags"]))

    def fit_list(self, event):
        self.list_canvas.itemconfigure(self.list_window, width=event.width)
        self.game_width = max(200, event.width - sum(width for _, _, width in LIST_COLUMNS))
        self.list_header.columnconfigure(0, minsize=self.game_width)
        for row in self.rows.values():
            row.fit(self.game_width)

    def scroll_list(self, units):
        if self.list_frame.winfo_height() > self.list_canvas.winfo_height():
            self.list_canvas.yview_scroll(units, "units")

    def select(self, entry_id):
        self.selected = entry_id
        self.list_canvas.focus_set()
        self.paint_rows()
        self.show_detail()

    def move_selection(self, step):
        if self.selected in self.shown_ids:
            index = self.shown_ids.index(self.selected) + step
            if 0 <= index < len(self.shown_ids):
                self.select(self.shown_ids[index])
                self.see(self.rows[self.selected].frame)

    def see(self, frame):
        self.root.update_idletasks()
        total = self.list_frame.winfo_height()
        visible = self.list_canvas.winfo_height()
        top = self.list_canvas.canvasy(0)
        if frame.winfo_y() < top:
            self.list_canvas.yview_moveto(frame.winfo_y() / total)
        elif frame.winfo_y() + frame.winfo_height() > top + visible:
            self.list_canvas.yview_moveto((frame.winfo_y() + frame.winfo_height() - visible) / total)

    def build_rows(self):
        signature = (self.vault.root, *((item["id"], tuple(item["tags"])) for item in self.items.values()))
        if signature == self.row_signature:
            return
        self.row_signature = signature
        for row in self.rows.values():
            row.frame.destroy()
        self.rows = {entry_id: GameRow(self, item) for entry_id, item in self.items.items()}
        for row in self.rows.values():
            row.fit(self.game_width)
        self.shown_ids = None
        self.rebuild_rows()

    def rebuild_rows(self):
        ids = [entry_id for entry_id, item in self.items.items() if self.matches(item)]
        if ids != self.shown_ids:
            self.shown_ids = ids
            for row in self.rows.values():
                row.frame.pack_forget()
            for entry_id in ids:
                self.rows[entry_id].frame.pack(fill="x", pady=(0, 1))
            if self.selected not in ids:
                self.selected = ids[0] if ids else None
            self.list_canvas.yview_moveto(0)
        self.paint_rows()

    def paint_rows(self):
        for entry_id in self.shown_ids or []:
            self.rows[entry_id].paint(self.items[entry_id], entry_id == self.selected)

    def show_detail(self):
        entry_id = self.selected
        item = self.items.get(entry_id)
        key = (entry_id, item["sha256"]) if item else None
        if key != self.detail_key:
            self.detail_key = key
            self.detail_id = entry_id
            self.art_id = None
            self.art.pack_forget()
            self.fill_text(item)
        if not item:
            for label in (self.name, self.state_label):
                label.configure(text="")
            self.action.state(["disabled"])
            return
        self.show_art(item)
        self.name.configure(text=item["name"])
        self.paint_action(item)

    def fill_text(self, item):
        text = self.text
        text.configure(state="normal")
        text.delete("1.0", "end")
        if item:
            self.write_detail(item)
        text.configure(state="disabled")
        text.yview_moveto(0)

    def write_detail(self, item):
        add = self.text.insert

        def heading(title):
            add("end", title.upper() + "\n", "heading")

        def row(key, value, *tags):
            add("end", key + "\t", ("key", "row"))
            add("end", value + "\n", ("row", *tags))

        def bullets(title, values, mark, style):
            if values:
                heading(title)
                for value in values:
                    add("end", mark + "  ", ("item", style))
                    add("end", value + "\n", "item")

        if item["tagline"]:
            add("end", item["tagline"] + "\n", "tagline")
        if item["tags"]:
            for tag in item["tags"]:
                add("end", " " + tag.replace(" ", " ") + " ", ("chip", "chips"))
                add("end", " ", "chips")
            add("end", "\n", "chips")

        heading("Thông tin")
        for key, value in (("Nhà phát triển", item["developer"]), ("Phát hành", item["release"]),
                           ("Dịch bởi", item["translator"])):
            if value:
                row(key, value)
        row("Phiên bản việt hóa", item["version"] or "-")
        if item["game_version"]:
            row("Phiên bản game", item["game_version"])
        else:
            row("Phiên bản game", "Miss", "missing_value")
        row("Title ID", item["title_id"])
        row("Dung lượng tải", format_size(item["size"]))

        if item["description"]:
            heading("Giới thiệu")
            for line in item["description"]:
                add("end", line + "\n", "body")
        if item["highlights"]:
            heading("Điểm nổi bật")
            for entry in item["highlights"]:
                if entry["title"]:
                    add("end", entry["title"] + "\n", "strong")
                if entry["text"]:
                    add("end", entry["text"] + "\n", "body")
        bullets("Đã việt hóa", item["done"], "✓", "yes")
        bullets("Giữ nguyên", item["kept"], "–", "no")
        if item["changelog"]:
            heading("Ghi chú")
            add("end", item["changelog"] + "\n", "body")
        if item["credit"]:
            heading("Credit")
            add("end", item["credit"] + "\n", "body")

    def paint_action(self, item):
        job = item["job"]
        active = bool(job) and job["phase"] != "error"
        self.action.state(["!disabled"])
        if job and job["phase"] == "error":
            message, label, accent = "Lỗi: " + job.get("message", ""), "Thử lại", True
        elif active:
            message, label, accent = status_text(item), "Huỷ", False
        elif item["status"] == "update":
            message, label, accent = status_text(item), "Cập nhật", True
        elif item["status"] == "ok":
            message, label, accent = "Đã tải, mới nhất", "Tải lại", False
        else:
            message, label, accent = "Chưa tải", "Tải về", True
        self.state_label.configure(text=message)
        self.action.configure(text=label, style="Accent.TButton" if accent else "TButton")
        if active:
            self.progress["value"] = 1000 * job["done"] / job["total"]
            if not self.progress.winfo_manager():
                self.progress.pack(fill="x", pady=(0, 6), before=self.action)
        else:
            self.progress.pack_forget()

    def show_art(self, item):
        image = self.art_cache.get((self.vault.root.name, item["id"]))
        if image is None or self.art_id == item["id"]:
            return
        self.art_id = item["id"]
        self.art_image = ImageTk.PhotoImage(image)
        self.art.configure(image=self.art_image)
        self.art.pack(before=self.name, anchor="w", pady=(0, 10))

    def preload_art(self):
        """Fetch and compose every banner in the background so selecting a game is instant."""
        vault = self.vault
        items = [item for item in self.items.values() if item["cover"] or item["background"]]
        signature = (vault.root.name, *((item["cover"], item["background"]) for item in items))
        if Image is None or signature == self.art_signature:
            return
        self.art_signature = signature
        art_dir = vault.art_dir
        art_dir.mkdir(parents=True, exist_ok=True)
        files = {art_file_name(item, kind): item[kind] for item in items for kind in ART_KINDS if item[kind]}
        known = vault._load_json(vault.art_index, {})
        for old in art_dir.iterdir():
            if old.name not in files or known.get(old.name) != files[old.name]:
                old.unlink(missing_ok=True)
        vault._write_json(vault.art_index, files)
        pending = queue.Queue()
        for item in items:
            pending.put(item)
        for _ in range(ART_WORKERS):
            threading.Thread(target=self.art_worker, args=(pending, art_dir), daemon=True).start()

    def art_worker(self, pending, art_dir):
        while True:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                return
            cover = self.load_image(item, "cover", art_dir)
            background = self.load_image(item, "background", art_dir)
            if cover or background:
                try:
                    self.art_cache[(art_dir.name, item["id"])] = compose_banner(cover, background, self.ART_SIZE)
                except Exception:
                    pass

    def load_image(self, item, kind, art_dir):
        url = item[kind]
        if not url:
            return None
        path = art_dir / art_file_name(item, kind)
        try:
            if not path.is_file():
                with http_get(url) as response:
                    data = response.read(MAX_IMAGE + 1)
                if len(data) > MAX_IMAGE:
                    return None
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                os.replace(temp, path)
            with Image.open(path) as image:
                return image.convert("RGB")
        except Exception:
            return None

    def refresh(self):
        snapshot = self.vault.snapshot()
        items = {item["id"]: item for item in snapshot["items"]}
        self.items = items
        genres = sorted({tag for item in items.values() for tag in item["tags"]}, key=str.lower)
        if genres != self.genres:
            self.genres = genres
            self.close_genre_panel()
            self.selected_genres &= set(genres)
            self.paint_genre_button()
            self.shown_ids = None
        self.paint_catalogs(snapshot)
        self.build_rows()
        self.paint_rows()
        self.preload_art()
        self.show_detail()
        self.paint_header(snapshot)
        self.paint_app_check()
        self.paint_app_update(snapshot["app_update"], snapshot["app_update_job"])
        self.root.after(400, self.refresh)

    def paint_catalogs(self, snapshot):
        folders = [catalog["folder"] for catalog in snapshot["catalogs"]]
        if folders != self.catalog_folders:
            self.catalog_folders = folders
            self.catalog_box.configure(values=[catalog["name"] for catalog in snapshot["catalogs"]])
        if snapshot["active"] in folders:
            self.catalog_box.current(folders.index(snapshot["active"]))
        else:
            self.catalog_box.set("")

    def catalog_changed(self):
        self.catalog_box.selection_clear()
        index = self.catalog_box.current()
        if index < 0:
            return
        if self.vault.busy():
            messagebox.showinfo("Đang tải", "Hãy đợi các file đang tải xong rồi đổi catalog.", parent=self.root)
            return
        self.vault.select_catalog(self.catalog_folders[index])
        self.check()

    def open_manager(self):
        if self.manager and self.manager.window.winfo_exists():
            self.manager.window.lift()
        else:
            self.manager = CatalogManager(self)

    def paint_header(self, snapshot):
        idle = [item for item in self.items.values() if not item["job"]]
        self.updates = [item["id"] for item in idle if item["status"] == "update"]
        missing = [item for item in idle if item["status"] == "missing"]
        self.missing = [item["id"] for item in missing]
        have = sum(item["status"] != "missing" for item in self.items.values())
        parts = [f"{have}/{len(self.items)} đã tải"]
        if self.updates:
            parts.append(f"{len(self.updates)} bản cập nhật mới")
        if snapshot["checked_at"]:
            parts.append("kiểm tra lúc " + datetime.fromtimestamp(snapshot["checked_at"]).strftime("%H:%M:%S"))
        self.summary.configure(text=" · ".join(parts))
        if self.updates:
            self.update_all_button.configure(text=f"Cập nhật tất cả ({len(self.updates)})")
            self.update_all_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.update_all_button.pack_forget()
        size = format_size(sum(item["size"] for item in missing)) if missing else ""
        self.get_all_button.configure(text=f"Tải tất cả ({size})" if size else "Tải tất cả")
        self.get_all_button.state(["!disabled"] if missing else ["disabled"])
        self.check_button.configure(text="Đang cập nhật…" if snapshot["checking"] else "Cập nhật catalog")
        self.check_button.state(["disabled"] if snapshot["checking"] else ["!disabled"])
        if not self.vault.catalog:
            self.summary.configure(text="Chưa có catalog")
            self.footer.configure(text="Bấm Quản lý để thêm catalog đầu tiên", style="Muted.TLabel")
        elif snapshot["error"]:
            self.footer.configure(text="Không cập nhật được catalog: " + snapshot["error"], style="Error.TLabel")
        else:
            self.footer.configure(text="Lưu vào: " + snapshot["dir"], style="Muted.TLabel")

    def check_app(self):
        self.app_check_button.state(["disabled"])
        self.app_check_button.configure(text="Đang kiểm tra…")
        threading.Thread(target=self.run_app_check, daemon=True).start()

    def run_app_check(self):
        self.app_check_result = self.vault.check_app_update()

    def paint_app_check(self):
        result = self.app_check_result
        if result is None:
            return
        self.app_check_result = None
        self.app_check_button.state(["!disabled"])
        self.app_check_button.configure(text="Kiểm tra cập nhật app")
        if result == "available":
            # Asking by hand should prompt again, even after the user declined this version.
            self.prompted_tag = None
            return
        title = "Kiểm tra cập nhật app"
        if result == "current":
            message = f"Bạn đang dùng bản mới nhất ({app_version()})."
        elif result == "unversioned":
            message = "Bản này không có số phiên bản nên không kiểm tra được."
        else:
            self.root.after_idle(messagebox.showerror, title, "Không kiểm tra được bản mới. Hãy thử lại sau.")
            return
        self.root.after_idle(messagebox.showinfo, title, message)

    def paint_app_update(self, update, job):
        self.app_update = update
        if not update:
            return
        phase = job["phase"] if job else None
        if phase == "done":
            restart_program()
            self.root.destroy()
            return
        if phase == "error":
            self.vault.app_update_job = None
            self.root.after_idle(self.report_update_error, job["message"])
        if phase == "downloading":
            label = f"Đang cập nhật {100 * job['done'] // job['total']}%"
        elif phase == "installing":
            label = "Đang cài bản mới"
        else:
            label = f"Có bản mới {update['tag']}"
        self.app_update_button.configure(text=label)
        self.app_update_button.state(["disabled"] if phase in ("downloading", "installing") else ["!disabled"])
        if not self.app_update_button.winfo_manager():
            self.app_update_button.pack(side="left", padx=(8, 0), after=self.genre_button)
        if update["tag"] != self.prompted_tag:
            self.prompted_tag = update["tag"]
            self.root.after_idle(self.prompt_app_update)

    def prompt_app_update(self):
        action = "Tải về và cập nhật ngay? Ứng dụng sẽ tự khởi động lại." if can_self_update(self.app_update) \
            else "Mở trang tải về để cập nhật?"
        if messagebox.askyesno("Có phiên bản mới", f"Đã có PatchVault {self.app_update['tag']}. {action}",
                               parent=self.root):
            self.start_app_update()

    def start_app_update(self):
        update = self.app_update
        if not can_self_update(update):
            open_in_system(update["url"])
        elif any(job["phase"] != "error" for job in self.vault.snapshot_jobs()):
            messagebox.showinfo("Đang tải", "Hãy đợi các file đang tải xong rồi cập nhật ứng dụng.", parent=self.root)
        else:
            self.vault.set_update_job(phase="queued", done=0, total=1)
            threading.Thread(target=self.vault.install_app_update, args=(update,), daemon=True).start()

    def report_update_error(self, message):
        text = f"Không cập nhật được: {message}\n\nMở trang tải về để cập nhật thủ công?"
        if messagebox.askyesno("Lỗi cập nhật", text, icon="warning", parent=self.root):
            open_in_system(self.app_update["url"])

    def act(self):
        item = self.items.get(self.detail_id)
        if not item:
            return
        job = item["job"]
        if job and job["phase"] != "error":
            self.vault.cancel(item["id"])
        else:
            self.vault.clear_error(item["id"])
            self.vault.enqueue([item["id"]])

    def update_all(self):
        self.vault.enqueue(self.updates)

    def get_all(self):
        self.vault.enqueue(self.missing)

    def check(self):
        threading.Thread(target=self.vault.check, daemon=True).start()

    def open_folder(self):
        self.vault.root.mkdir(parents=True, exist_ok=True)
        open_in_system(self.vault.root)

    def close(self):
        active = [job for job in self.vault.snapshot_jobs() if job["phase"] != "error"]
        if self.vault.app_update_job:
            active.append(self.vault.app_update_job)
        if active and not messagebox.askyesno(
                "Đang tải", "Vẫn còn file đang tải. Thoát bây giờ? Lần sau có thể tải tiếp từ phần đã tải."):
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


class CatalogManager:
    """Window that adds and removes catalogs."""

    def __init__(self, app):
        self.app = app
        self.vault = app.vault
        self.result = None
        self.window = tk.Toplevel(app.root, bg=COLORS["bg"])
        self.window.title("Quản lý catalog")
        self.window.geometry("640x400")
        self.window.minsize(520, 340)
        self.window.transient(app.root)
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(frame, bg=COLORS["field"], fg=COLORS["ink"], bd=0, highlightthickness=0,
                                  selectbackground=COLORS["select"], selectforeground=COLORS["ink"],
                                  activestyle="none", font=app.font)
        self.listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda _: self.paint_buttons())
        self.remove_button = ttk.Button(frame, text="Xoá khỏi danh sách", command=self.remove)
        self.remove_button.grid(row=1, column=0, columnspan=2, sticky="e", pady=(6, 12))
        self.name = tk.StringVar()
        self.url = tk.StringVar()
        ttk.Label(frame, text="Tên", style="Muted.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(frame, textvariable=self.name).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(frame, text="URL", style="Muted.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        url_entry = ttk.Entry(frame, textvariable=self.url)
        url_entry.grid(row=3, column=1, sticky="ew", pady=3)
        url_entry.bind("<Return>", lambda _: self.add())
        self.status = ttk.Label(frame, style="Muted.TLabel", wraplength=460, justify="left")
        self.status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.add_button = ttk.Button(frame, text="Thêm catalog", style="Accent.TButton", command=self.add)
        self.add_button.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.fill_list()
        url_entry.focus_set()

    def fill_list(self):
        self.listbox.delete(0, "end")
        for catalog in self.vault.catalogs:
            self.listbox.insert("end", f"{catalog['name']}  ·  {catalog['url']}")
        self.paint_buttons()

    def paint_buttons(self):
        self.remove_button.state(["!disabled"] if self.listbox.curselection() else ["disabled"])

    def add(self):
        if self.result is not None or "disabled" in self.add_button.state():
            return
        self.add_button.state(["disabled"])
        self.status.configure(text="Đang đọc catalog…", style="Muted.TLabel")
        threading.Thread(target=self.run_add, args=(self.name.get(), self.url.get()), daemon=True).start()
        self.window.after(150, self.wait_for_add)

    def run_add(self, name, url):
        try:
            catalog = self.vault.add_catalog(name, url)
            self.result = ("ok", f"Đã thêm {catalog['name']}.")
        except (OSError, ValueError, http.client.HTTPException) as error:
            self.result = ("error", str(error) or type(error).__name__)

    def wait_for_add(self):
        if not self.window.winfo_exists():
            return
        if self.result is None:
            self.window.after(150, self.wait_for_add)
            return
        state, message = self.result
        self.result = None
        self.add_button.state(["!disabled"])
        self.status.configure(text=message, style="Muted.TLabel" if state == "ok" else "Error.TLabel")
        if state == "ok":
            self.name.set("")
            self.url.set("")
            self.fill_list()

    def remove(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        catalog = self.vault.catalogs[selected[0]]
        if self.vault.busy() and catalog is self.vault.catalog:
            self.status.configure(text="Hãy đợi các file đang tải xong.", style="Error.TLabel")
            return
        if messagebox.askyesno("Xoá catalog", f"Xoá {catalog['name']} khỏi danh sách? File đã tải vẫn được giữ lại.",
                               parent=self.window):
            self.vault.remove_catalog(catalog["folder"])
            self.fill_list()


def compose_banner(cover, background, size):
    """Background art filling the banner, darkened, with the cover art on top."""
    width, height = size
    base = ImageOps.fit(background or cover, size, Image.LANCZOS)
    if background is None:
        base = base.filter(ImageFilter.GaussianBlur(10))
    base = Image.blend(base, Image.new("RGB", size), 0.4)
    if cover is not None:
        thumb = cover.copy()
        thumb.thumbnail((width // 2, height - 24), Image.LANCZOS)
        top = (height - thumb.height) // 2
        ImageDraw.Draw(base).rectangle((11, top - 1, 12 + thumb.width, top + thumb.height), outline="#00000080")
        base.paste(thumb, (12, top))
    return base


def periodic_check(vault):
    while True:
        vault.check()
        vault.check_app_update()
        time.sleep(CHECK_RETRY if vault.check_error else CHECK_INTERVAL)


def run_check(vault):
    vault.check()
    vault.check_app_update()
    if vault.app_update:
        print(f"Có phiên bản PatchVault mới: {vault.app_update['tag']} ({vault.app_update['url']})")
    if not vault.catalog:
        print("Chưa có catalog. Hãy mở ứng dụng để thêm catalog.")
        return 0
    if vault.check_error:
        print(f"Lỗi kiểm tra catalog: {vault.check_error}", file=sys.stderr)
        return 1
    updates = [entry for entry in vault.items if vault.status_of(entry) == "update"]
    missing = sum(vault.status_of(entry) == "missing" for entry in vault.items)
    for entry in updates:
        old = vault.state[entry["id"]].get("version") or "?"
        print(f"Có cập nhật: {entry['name']} ({old} -> {entry['version'] or '?'})")
    print(f"{vault.catalog['name']}: {len(updates)} bản cập nhật, {missing} game chưa tải, "
          f"tổng {len(vault.items)} game.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Tải và theo dõi các bản Việt hoá game Switch.")
    parser.add_argument("--dir", default=str(base_dir()), help="thư mục gốc của dữ liệu, file tải về nằm trong downloaded/")
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra cập nhật rồi thoát")
    args = parser.parse_args()

    vault = Vault(args.dir)
    if args.check:
        return run_check(vault)
    remove_old_program()
    if tk is None:
        print("Máy này chưa có Tkinter. Hãy cài gói tk (Arch) hoặc python3-tk (Debian/Ubuntu).", file=sys.stderr)
        return 1

    threading.Thread(target=periodic_check, args=(vault,), daemon=True).start()
    for _ in range(WORKERS):
        threading.Thread(target=vault.worker, daemon=True).start()
    app = App(vault)
    if not vault.catalogs:
        app.root.after(300, app.open_manager)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
