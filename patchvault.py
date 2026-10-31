#!/usr/bin/env python3
"""PatchVault: download, track and repackage game mods.

Needs Python 3.8+ with Tkinter; Pillow is optional and enables artwork. Catalogs are added
from the app window, and each one keeps its own downloads, artwork and state.

Every patch is repackaged as
    downloaded/<catalog>/<Game name> - [<mod name>]/[<game version>-unreviewed] [<mod name>].zip
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
    from tkinter import filedialog, font as tkfont, ttk
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
INTERNAL_VERSION = "unreviewed"
MISSING_VERSION = "miss"
MAX_UNPACKED = 32 * 1024 ** 3
MAX_IMAGE = 8 * 1024 * 1024
MAX_CATALOG = 32 * 1024 * 1024
CATALOGS_DIR = ".catalogs"
MAX_UPDATE = 300 * 1024 * 1024
TITLES_URL = "https://raw.githubusercontent.com/blawar/titledb/master/US.en.json"
TITLES_FILE = "_titles.json"
BACKUP_DIR = "backup"
TITLES_MAX_AGE = 7 * 24 * 3600
FIRST_GAME_TITLE = 0x0100000000010000
MAX_TITLES = 400 * 1024 * 1024
PROGRAM_NAME = "PatchVault"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
ART_KINDS = ("cover", "background")
DOWNLOAD_DIR = "downloaded"
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

CONFIG_KEYS = ("PATCHVAULT_RELEASES_URL",)
VERSION_KEY = "PATCHVAULT_VERSION"
ROMAN_NUMERAL = re.compile(r"(?=[IVX])X{0,3}(?:IX|IV|V?I{0,3})")
WRAPPER_NAMES = {"atmosphere", "contents", "titles", "mods", "mod", "switch", "sdcard"}

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_GDRIVE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
TITLE_ID = re.compile(r"^[0-9A-Fa-f]{16}$")
ASSET_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

DISPLAY_VERSION = re.compile(r"v?\d+(?:\.\d+)*")
# File names hold words like "2" or "1080p", so only 1.2 or v2 count as a version here.
# A source named [1.1.1-v262144] already gives the game's displayed and internal versions.
VERSION_PAIR = re.compile(r"\[(v?\d+(?:\.\d+)*)-(v\d+)\]", re.IGNORECASE)
VERSION_IN_NAME = re.compile(r"(?<![^\W_])(?:v?\d+(?:\.\d+)+|v\d+)(?![^\W_])", re.IGNORECASE)
TRAILING_TAG = re.compile(r"\[([^\[\]]+)\]\s*$")
TRAILING_FOLDER_TAG = re.compile(r"\S.*\s-\s*\[([^\[\]]+)\]\s*$")
LEADING_TAG = re.compile(r"\[([^\[\]]+)\]\s*-?\s*[^\s\[]")
MOD_WORDS = re.compile(r"[\s._-]*\b(?:vi[ệe]t\s*h[oó][aá]|vietnamese|vietsub|patch|mod)\b[\s._-]*$", re.IGNORECASE)


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


# The canonical fields PatchVault works with, and the source keys we recognise for each
# without being told. Catalogs from different projects name the same thing differently
# (English, Vietnamese, nested under a files array), so we try each alias in turn. A field
# mapping supplied per catalog overrides this. Dotted aliases dig into nested dicts/lists,
# e.g. "files.0.url" reads the first file's url. The first alias of each field is the key
# PatchVault's own catalogs use, so a catalog with no surprises parses exactly as before.
FIELD_ALIASES = {
    "id": ["id", "slug", "key", "ma", "ma_game", "game_id"],
    "name": ["name", "title", "ten_game", "ten", "game_name"],
    "title_id": ["title_id", "titleid", "tid", "title_id_game", "ma_tua"],
    "version": ["mod_version", "version", "phien_ban", "ver", "mod_ver"],
    "game_version": ["game_version", "phien_ban_game", "phien_ban_yeu_cau", "game_ver"],
    "sha256": ["sha256", "sha", "hash", "checksum", "sha_256",
               "files.0.sha256", "tap_tin.0.sha256", "file.sha256"],
    "size": ["size", "dung_luong", "dung_luong_tap_tin", "filesize", "file_size", "bytes",
             "files.0.size", "tap_tin.0.dung_luong", "file.size"],
    "url": ["url", "download", "download_url", "link", "tai_ve", "duong_dan",
            "files.0.url", "tap_tin.0.tai_ve", "tap_tin.0.url", "file.url"],
    "mirrors": ["mirrors", "mirror", "links", "guong"],
    "cover": ["cover", "cover_url", "image", "thumbnail", "anh", "anh_bia", "poster"],
    "background": ["background", "banner", "anh_nen", "bg"],
    "tagline": ["tagline", "slogan", "mo_ta_ngan"],
    "developer": ["developer", "dev", "nha_phat_trien", "studio"],
    "release": ["release", "release_date", "ngay_phat_hanh"],
    "translator": ["translator", "nguoi_dich", "dich_gia", "nhom_dich"],
    "tags": ["tags", "genres", "the_loai", "ten_the_loai", "categories", "danh_muc"],
    "description": ["description", "desc", "mo_ta"],
    "highlights": ["highlights", "diem_noi_bat"],
    "done": ["done", "da_lam"],
    "kept": ["kept", "giu_lai"],
    "changelog": ["changelog", "lich_su", "ghi_chu"],
    "credit": ["credit", "credits", "cam_on"],
}
# Keys whose value is the array of games. Checked in order; a top-level JSON array is used
# as-is. "__root__" in a mapping overrides this.
ROOT_ALIASES = ["mods", "games", "items", "data", "list", "catalog", "patches", "danh_sach"]
# Fields the "advanced" panel of the catalog dialog lets the user map by hand.
MAPPABLE_FIELDS = [
    ("__root__", "Mảng game (gốc)"), ("id", "Mã (id)"), ("name", "Tên game"),
    ("title_id", "Title ID"), ("version", "Phiên bản việt hoá"), ("game_version", "Phiên bản game"),
    ("url", "Link tải"), ("sha256", "SHA-256"), ("size", "Dung lượng"), ("cover", "Ảnh bìa"),
]


def dig(obj, path):
    """Follow a dotted path through dicts and lists, returning None if any step is missing."""
    for part in str(path).split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit() and -len(obj) <= int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
        if obj is None:
            return None
    return obj


def pick(raw, field, mapping):
    """Read one canonical field: an explicit mapping wins, otherwise try the known aliases."""
    if mapping and mapping.get(field):
        return dig(raw, mapping[field])
    for alias in FIELD_ALIASES.get(field, [field]):
        value = dig(raw, alias)
        if value is not None:
            return value
    return None


def as_text(value):
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def as_list(value):
    return value if isinstance(value, list) else []


def as_size(value):
    """Coerce a size that may arrive as an int, float, or a string like "1,234"."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = re.sub(r"[,\s_]", "", value)
        if text.isdigit():
            return int(text)
    return 0


def clean_mapping(mapping):
    """Keep only the non-empty overrides, so an untouched advanced panel means pure heuristic."""
    if not isinstance(mapping, dict):
        return {}
    return {field: key.strip() for field, key in mapping.items()
            if isinstance(key, str) and key.strip()}


def resolve_urls(primary, mirrors):
    urls = [https_or_empty(primary)]
    for mirror in as_list(mirrors):
        if isinstance(mirror, str) and mirror.startswith("gdrive:"):
            file_id = mirror[len("gdrive:"):]
            if SAFE_GDRIVE.match(file_id):
                urls.append(GDRIVE_URL + file_id)
        else:
            urls.append(https_or_empty(mirror))
    return [url for url in dict.fromkeys(urls) if url]


def normalize(raw, mapping=None):
    """Turn one catalog entry into PatchVault's own shape. The catalog is remote data, so
    nothing in it is trusted. An id and at least one https download are required; everything
    else, SHA-256 and size included, is optional and features degrade when it is absent."""
    if not isinstance(raw, dict):
        return None
    get = lambda field: pick(raw, field, mapping)
    name = as_text(get("name")).strip()
    raw_id = get("id")
    # A catalog keyed only by name gets an id derived from it; an id that is present but
    # unsafe is rejected rather than papered over.
    entry_id = art_slug(name)[:64] if raw_id is None else as_text(raw_id).strip()
    if not SAFE_ID.match(entry_id):
        return None
    urls = resolve_urls(get("url"), get("mirrors"))
    if not urls:
        return None
    sha = as_text(get("sha256")).strip().lower()
    sha = sha if SAFE_SHA.match(sha) else ""
    size = max(0, as_size(get("size")))
    return {
        "id": entry_id,
        "name": name or entry_id,
        "version": as_text(get("version")),
        "game_version": as_text(get("game_version")),
        "title_id": as_text(get("title_id")),
        "tagline": as_text(get("tagline")),
        "developer": as_text(get("developer")),
        "release": as_text(get("release")),
        "translator": as_text(get("translator")),
        "tags": [as_text(tag) for tag in as_list(get("tags")) if as_text(tag)],
        "description": [as_text(line) for line in as_list(get("description"))],
        "highlights": [{"title": as_text(dig(entry, "title")), "text": as_text(dig(entry, "text"))}
                       for entry in as_list(get("highlights")) if isinstance(entry, dict)],
        "done": [as_text(line) for line in as_list(get("done"))],
        "kept": [as_text(line) for line in as_list(get("kept"))],
        "changelog": as_text(get("changelog")),
        "credit": as_text(get("credit")),
        "cover": https_or_empty(get("cover")),
        "background": https_or_empty(get("background")),
        "sha256": sha,
        "size": size,
        "urls": urls,
    }


def find_rows(data, mapping):
    """Locate the array of games: a mapped key, a known wrapper key, or a bare top-level list."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    if mapping and mapping.get("__root__"):
        rows = dig(data, mapping["__root__"])
        return rows if isinstance(rows, list) else None
    for key in ROOT_ALIASES:
        if isinstance(data.get(key), list):
            return data[key]
    return None


def fetch_catalog(url, mapping=None):
    """Return the raw bytes and parsed items of the catalog at url."""
    separator = "&" if "?" in url else "?"
    with http_get(f"{url}{separator}t={int(time.time())}") as response:
        raw = response.read(MAX_CATALOG + 1)
    if len(raw) > MAX_CATALOG:
        raise ValueError("catalog quá lớn")
    return raw, parse_catalog(json.loads(raw), mapping)


def parse_catalog(data, mapping=None):
    rows = find_rows(data, mapping)
    if rows is None:
        raise ValueError("file không phải catalog")
    items = []
    seen = set()
    for raw in rows:
        entry = normalize(raw, mapping)
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


def safe_name(name):
    cleaned = re.sub(r'[:/\\?*"<>|\x00-\x1f]+', " ", name)
    return re.sub(r"\s+", " ", cleaned).strip(" .")[:120]


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


def plan_repack(infos, title_id):
    """Map source zip members to contents/<title id>/... names, in source order. Every title
    in the zip stays: a pack's extra titles, such as a system font, belong to the game."""
    if not TITLE_ID.match(title_id):
        raise RuntimeError(f"title id không hợp lệ: {title_id!r}")
    plan = {}
    total = 0
    found = False
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
            continue
        if index + 1 == len(parts):
            raise RuntimeError(f"cấu trúc lạ trong zip: {name}")
        ours = parts[index].lower() == title_id.lower()
        found = found or ours
        arcname = "/".join(["contents", title_id if ours else parts[index].upper(), *parts[index + 1:]])
        if arcname.lower() in {key.lower() for key in plan}:
            raise RuntimeError(f"file trùng tên trong zip: {arcname}")
        plan[arcname] = info
        total += info.file_size
    if not found:
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


def add_member(output, arcname, date_time, size, reader, written, on_bytes=None):
    """Stream one file into a vault zip, writing its parent folders the first time they appear."""
    folders = arcname.split("/")[:-1]
    for depth in range(1, len(folders) + 1):
        folder = "/".join(folders[:depth]) + "/"
        if folder not in written:
            written.add(folder)
            output.writestr(member_info(folder, date_time, 0o40755, zipfile.ZIP_STORED), b"")
    info = member_info(arcname, date_time, 0o100644, zipfile.ZIP_DEFLATED)
    info.file_size = size
    with output.open(info, "w") as writer:
        for block in iter(lambda: reader.read(CHUNK), b""):
            writer.write(block)
            if on_bytes is not None:
                on_bytes(len(block))


def repack(source, target, title_id, cancel=None, on_bytes=None):
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
                with original.open(member) as reader:
                    add_member(output, arcname, member.date_time, member.file_size, reader, written, on_bytes)
            expected = {name: member.CRC for name, member in plan.items()}
        with zipfile.ZipFile(temp) as result:
            actual = {i.filename: i.CRC for i in result.infolist() if not i.is_dir()}
            if actual != expected or result.testzip() is not None:
                raise RuntimeError("zip đóng gói lại không khớp với zip gốc")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def folder_files(folder, prefix=""):
    """Files of a loose title ID folder, as (path, name inside the folder, under prefix)."""
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"có liên kết tượng trưng: {path.name}")
        if path.is_file() and path.name.lower() not in JUNK_NAMES:
            files.append((path, (Path(prefix) / path.relative_to(folder)).as_posix()))
    if not files:
        raise RuntimeError("thư mục không có file nào")
    return files


def zip_date(path):
    """A zip cannot store dates outside 1980-2107."""
    stamp = time.localtime(path.stat().st_mtime)[:6]
    return min(max(stamp, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))


def repack_folder(folders, target, cancel=None, on_bytes=None):
    """Pack loose title ID folders into the same layout as repack."""
    temp = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        expected = {}
        written = set()
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
            for path, relative in (pair for folder in folders for pair in folder_files(folder, folder.name.upper())):
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                arcname = f"contents/{relative}"
                size = path.stat().st_size
                with open(path, "rb") as reader:
                    add_member(output, arcname, zip_date(path), size, reader, written, on_bytes)
                expected[arcname] = size
        with zipfile.ZipFile(temp) as result:
            actual = {i.filename: i.file_size for i in result.infolist() if not i.is_dir()}
            if actual != expected or result.testzip() is not None:
                raise RuntimeError("zip đóng gói không khớp với thư mục gốc")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def find_mods(root):
    """Every mod under root, whatever the structure around it, as (kind, paths): one zip, or
    the title ID folders that sit side by side. atmosphere/ and contents/ are only wrappers,
    so walking down to the title IDs covers them. Side by side means one pack, except in
    root itself, which holds whatever the user gathered there."""
    found = []
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        titles = [Path(folder) / name for name in dirs if TITLE_ID.match(name)]
        if titles:
            found.extend(("folder", [title]) for title in titles) if Path(folder) == root \
                else found.append(("folder", titles))
        dirs[:] = [name for name in dirs if not TITLE_ID.match(name) and name != "__MACOSX"]
        found.extend(("zip", [Path(folder) / name]) for name in sorted(files) if name.lower().endswith(".zip"))
    return found


def zip_titles(infos):
    """Title IDs found in a zip, each once, in upper case."""
    titles = []
    for info in infos:
        parts = info.filename.replace("\\", "/").split("/")
        if parts[0] != "__MACOSX":
            title = next((part.upper() for part in parts[:-1] if TITLE_ID.match(part)), None)
            if title and title not in titles:
                titles.append(title)
    return titles


def clean_mod_name(text):
    """The game name inside a file or folder name, or "" when there is none."""
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", unicodedata.normalize("NFC", text))
    text = VERSION_IN_NAME.sub(" ", text).replace("_", " ")
    text = re.sub(r"\s+", " ", MOD_WORDS.sub("", text)).strip(" .-")
    return "" if text.lower() in WRAPPER_NAMES or TITLE_ID.match(text) else text


def internal_version(kind, path, root, version):
    """The internal version a source gives for this game version, if it gives one."""
    relative = path.relative_to(root)
    names = ([relative.stem] if kind == "zip" else []) + [parent.name for parent in relative.parents if parent.name]
    pairs = (VERSION_PAIR.search(name) for name in names)
    wanted = display_version(version).lstrip("vV")
    return next((pair.group(2) for pair in pairs if pair and pair.group(1).lstrip("vV") == wanted), INTERNAL_VERSION)


def guess_details(kind, path, root):
    """Name and version from the file name and the folders around it, nearest first."""
    relative = path.relative_to(root)
    names = ([relative.stem] if kind == "zip" else []) + [parent.name for parent in relative.parents if parent.name]
    name = next(filter(None, map(clean_mod_name, names)), "")
    versions = (VERSION_IN_NAME.search(candidate) for candidate in names)
    version = next((match.group(0) for match in versions if match), "")
    return name, version


def source_mod_name(kind, path, root):
    """The mod name a source already carries: a trailing [tag] on a zip, or a folder named
    [tag] - Game or Game - [tag]. Tags holding a version are not names, and neither is a
    folder that is only a [tag], which groups mods rather than naming one."""
    relative = path.relative_to(root)
    tags = [TRAILING_TAG.search(relative.stem)] if kind == "zip" else []
    for parent in relative.parents:
        if parent.name:
            tags += [LEADING_TAG.match(parent.name), TRAILING_FOLDER_TAG.search(parent.name)]
    for tag in filter(None, tags):
        name = safe_name(tag.group(1))
        if name and not VERSION_IN_NAME.search(name) and not name.endswith(f"-{INTERNAL_VERSION}"):
            return name
    return ""


def parse_titles(data):
    """Reduce a titledb file, keyed by eShop id, to {title ID: game name}."""
    names = {}
    for row in data.values() if isinstance(data, dict) else ():
        if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("name"), str):
            if TITLE_ID.match(row["id"]) and row["name"].strip():
                names.setdefault(row["id"].upper(), row["name"].strip())
    return names


def title_name(names, title):
    """The game's name. Update title IDs end in 800, and share the base game's ID otherwise."""
    name = names.get(title) or names.get(title[:-3] + "000") or ""
    return re.sub(r"[™®©]", "", name)


def tidy_capitals(name):
    """Turn an all-capitals name into Title Case, word by word. Roman numerals and title IDs stay."""
    def tidy(word):
        text = word.group(0)
        if TITLE_ID.match(text):
            return text
        return re.sub(r"[^\W\d_]+", lambda part: part.group(0) if ROMAN_NUMERAL.fullmatch(part.group(0))
                      else part.group(0).capitalize(), text)

    return re.sub(r"[^\s-]+", tidy, name) if name.isupper() else name


def strip_accents(text):
    """Pokémon to Pokemon. Marks on other scripts stay, since they change the letter there."""
    kept = []
    for char in unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D")):
        if not (unicodedata.category(char) == "Mn" and kept and ord(kept[-1]) < 0x250):
            kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def clean_game_name(name):
    """A game name for a folder: letters without accents, digits, spaces and hyphens only.
    Slashes separate words, so they become spaces; other symbols simply go. All-capitals
    names are tidied too."""
    text = re.sub(r"[/\\|]+", " ", strip_accents(name))
    text = "".join(char for char in text if char in " -" or char.isspace() or unicodedata.category(char)[0] in "LNM")
    text = re.sub(r"\s+", " ", text).strip(" -")[:120]
    return tidy_capitals(text) or safe_name(name)


def game_folder(name, mod):
    return f"{clean_game_name(name)} - [{mod}]"


def normalized_path(name, version, mod, internal=INTERNAL_VERSION):
    """Path of a normalized mod, relative to the folder it is saved in."""
    return Path(game_folder(name, mod)) / f"[{display_version(version)}-{internal}] [{mod}].zip"


def is_game_title(title):
    """Below this are the console's own titles, such as system fonts."""
    return int(title, 16) >= FIRST_GAME_TITLE


def main_title(titles, names, known):
    """The title that names a pack: one known to be a game, else any that looks like a game."""
    games = [title for title in titles if is_game_title(title)]
    return next((title for title in games if title_name(names, title) or title in known), None) \
        or next(iter(games), None) or titles[0]


def title_plan(kind, paths, titles, scan, message=None, size=0):
    """One output zip. The name comes from the title database, then a catalog listing the
    title, then the file name; "guessed" marks a name that may change to avoid a clash."""
    title = main_title(titles, scan["names"], scan["known"]) if titles else ""
    guess_name, guess_version = guess_details(kind, paths[0], scan["root"])
    entry = scan["known"].get(title)
    official = title_name(scan["names"], title) or (entry and entry["name"])
    name = official or guess_name or title
    version = (entry and entry["game_version"]) or guess_version
    source = paths[0] if len(paths) == 1 else paths[0].parent
    mod = source_mod_name(kind, paths[0], scan["root"]) or scan["mod"]
    internal = internal_version(kind, paths[0], scan["root"], version)
    return {"kind": kind, "source": source, "paths": paths, "title_id": title, "extra_titles": len(titles) - 1,
            "name": name, "version": version, "mod": mod, "internal": internal,
            "stored": paths[0].stat().st_size if kind == "zip" else size,
            "guessed": not official, "target": scan["dest"] / normalized_path(name, version, mod, internal),
            "status": "error" if message else "ready", "message": message or "", "size": size, "written": 0}


def separate_clashes(plans, scan):
    """Titles with guessed names can land on one path, such as two titles in one pack. Keep
    them apart with the title ID. The same title twice is a real duplicate and stays one."""
    groups = {}
    for plan in plans:
        groups.setdefault(str(plan["target"]).casefold(), []).append(plan)
    for group in groups.values():
        if len({plan["title_id"] for plan in group}) > 1:
            for plan in group:
                if plan["guessed"]:
                    plan["name"] = f"{plan['name']} {plan['title_id']}"
                    plan["target"] = scan["dest"] / normalized_path(
                        plan["name"], plan["version"], plan["mod"], plan["internal"])


def mark_conflicts(plans):
    """Normalizing never overwrites: a file that is already there keeps its source in place."""
    seen = set()
    for plan in plans:
        if plan["status"] != "ready":
            continue
        key = str(plan["target"]).casefold()
        if plan["target"].exists():
            plan["status"], plan["message"] = "conflict", "Đã có file này ở thư mục lưu"
        elif key in seen:
            plan["status"], plan["message"] = "conflict", "Trùng với một mod khác trong lần quét này"
        seen.add(key)


def plan_mods(root, dest, known, names, mod):
    """Decide what normalizing would do. Nothing on disk changes here."""
    scan = {"root": root, "dest": dest, "known": known, "names": names, "mod": mod}
    plans = []
    for kind, paths in find_mods(root):
        if kind == "folder":
            titles = [path.name.upper() for path in paths]
            try:
                size = sum(file.stat().st_size for path in paths for file, _ in folder_files(path))
                plans.append(title_plan(kind, paths, titles, scan, size=size))
            except (OSError, RuntimeError) as error:
                plans.append(title_plan(kind, paths, titles, scan, str(error)))
            continue
        try:
            with zipfile.ZipFile(paths[0]) as archive:
                infos = archive.infolist()
        except (OSError, zipfile.BadZipFile) as error:
            plans.append(title_plan(kind, paths, [], scan, f"không đọc được zip: {error}"))
            continue
        titles = zip_titles(infos)
        if not titles:
            plans.append(title_plan(kind, paths, [], scan, "không tìm thấy thư mục Title ID"))
            continue
        plan = title_plan(kind, paths, titles, scan)
        try:
            plan["size"] = sum(info.file_size for info in plan_repack(infos, plan["title_id"]).values())
        except RuntimeError as error:
            plan.update(status="error", message=str(error))
        plans.append(plan)
    separate_clashes(plans, scan)
    mark_conflicts(plans)
    return plans


def free_path(path):
    """path, or the first numbered variant of it that is not taken."""
    candidate, number = path, 2
    while candidate.exists():
        name = f"{path.name} ({number})" if path.is_dir() else f"{path.stem} ({number}){path.suffix}"
        candidate, number = path.with_name(name), number + 1
    return candidate


def back_up(path, root, backup):
    """Move a processed source into backup at the same relative path, then remove any wrapper
    folders it leaves empty, up to root."""
    target = free_path(backup / path.relative_to(root))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    for parent in path.parents:
        if parent == root or root not in parent.parents:
            break
        try:
            parent.rmdir()
        except OSError:
            break
    return target


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def log_line(text, level):
    """A log entry: level is info, ok, warn or error, and sets its color."""
    return level, f"{datetime.now():%H:%M:%S}  {text}"


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
        self.download_log = []
        self.batch = self.new_batch()
        self.batch_number = 0
        self.checked_at = None
        self.check_error = None
        self.checking = False
        self.app_update = None
        self.app_update_job = None
        self.normalize_job = {"phase": "idle", "plans": [], "done": 0, "total": 0, "message": "", "failed": False,
                              "bytes_done": 0, "bytes_total": 0, "log": [], "runs": 0}
        self.normalize_cancel = threading.Event()
        registry = self._load_json(self.registry_path, {})
        titles = self._load_json(self.catalogs_dir / TITLES_FILE, {})
        self.titles = titles if isinstance(titles, dict) else {}
        self.mod_name = safe_name(str(registry.get("mod_name") or ""))
        self.download_dir = str(registry.get("download_dir") or "")
        self.normalize_dest = str(registry.get("normalize_dest") or "")
        self.catalogs = registry.get("catalogs", [])
        active = next((c for c in self.catalogs if c["folder"] == registry.get("active")), None)
        self._use_catalog(active or (self.catalogs[0] if self.catalogs else None))

    def _use_catalog(self, catalog):
        """Point the vault at one catalog. Every catalog has its own files and state."""
        self.catalog = catalog
        folder = catalog["folder"] if catalog else "_none"
        self.root = self._download_root() / folder
        self.art_dir = self.base / "covers" / folder
        self.art_index = self.catalogs_dir / f"{folder}.covers.json"
        self.state_path = self.catalogs_dir / f"{folder}.state.json"
        self.cache_path = self.catalogs_dir / f"{folder}.json"
        self.state = self._load_json(self.state_path, {})
        self.items = []
        self.checked_at = None
        self.check_error = None
        try:
            mapping = clean_mapping(catalog.get("mapping")) if catalog else None
            self.items = self._named(parse_catalog(json.loads(self.cache_path.read_bytes()), mapping))
        except (OSError, ValueError):
            pass

    def _named(self, items):
        """Show the title database's name for each game, keeping the catalog's as a fallback."""
        named = []
        for item in items:
            catalog_name = item.get("catalog_name", item["name"])
            name = title_name(self.titles, item["title_id"].upper()) or catalog_name
            named.append(dict(item, name=name, catalog_name=catalog_name))
        return named

    def _save_registry(self):
        active = self.catalog["folder"] if self.catalog else None
        self._write_json(self.registry_path, {"active": active, "catalogs": self.catalogs,
                                              "mod_name": self.mod_name, "download_dir": self.download_dir,
                                              "normalize_dest": self.normalize_dest})

    def _download_root(self):
        return Path(self.download_dir).expanduser().resolve() if self.download_dir else self.base / DOWNLOAD_DIR

    def set_download_dir(self, text):
        """Choose where downloads go; blank means the default. Workers read self.root, so it
        stays put while anything is downloading."""
        text = text.strip()
        with self.lock:
            if text == self.download_dir or self.busy():
                return
            self.download_dir = text
            self.root = self._download_root() / self.root.name
            self._save_registry()

    def set_normalize_dest(self, text):
        text = text.strip()
        with self.lock:
            if text == self.normalize_dest or self.normalizing():
                return
            self.normalize_dest = text
            self._save_registry()
            self._drop_scan("Thư mục đích đã đổi, hãy bấm Quét lại.")

    def _drop_scan(self, message):
        if self.normalize_job["plans"]:
            self._set_normalize(phase="idle", plans=[], done=0, total=0, failed=False, message=message)

    def set_mod_name(self, name):
        """Save the one mod name used by downloads and normalizing. A reviewed scan shows
        paths built from the old name, so it has to be redone."""
        name = safe_name(name)
        with self.lock:
            if name == self.mod_name or self.normalizing():
                return
            self.mod_name = name
            self._save_registry()
            self._drop_scan("Tên mod đã đổi, hãy bấm Quét lại.")

    def add_catalog(self, name, url, mapping=None):
        """Add a catalog after confirming that the URL really serves one, then switch to it.
        An optional field mapping tells the parser how to read a catalog whose keys the
        heuristics do not recognise."""
        url = url.strip()
        if not url.startswith("https://"):
            raise ValueError("URL phải bắt đầu bằng https://")
        mapping = clean_mapping(mapping)
        with self.lock:
            if any(catalog["url"] == url for catalog in self.catalogs):
                raise ValueError("catalog này đã có trong danh sách")
        raw, _ = fetch_catalog(url, mapping)
        name = name.strip() or urlsplit(url).hostname or "catalog"
        with self.lock:
            if self.busy():
                raise ValueError("hãy đợi các file đang tải xong rồi thêm catalog")
            taken = {catalog["folder"] for catalog in self.catalogs}
            slug = art_slug(name) or "catalog"
            folder, number = slug, 2
            while folder in taken:
                folder, number = f"{slug}-{number}", number + 1
            catalog = {"name": name, "url": url, "folder": folder, "mapping": mapping}
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
            raw, items = fetch_catalog(catalog["url"], clean_mapping(catalog.get("mapping")))
            with self.lock:
                # The user may have switched catalogs while this one was loading.
                if self.catalog is catalog:
                    self._write_bytes(self.cache_path, raw)
                    self.items = self._named(items)
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

    def game_folders(self, entry):
        """Where a game's zips may be. Saves made before the title database knew the game
        are under the catalog's name, and older ones kept symbols that names now lose."""
        names = {entry["name"], entry.get("catalog_name", entry["name"])}
        folders = {game_folder(name, self.mod_name) for name in names}
        folders |= {f"{safe_name(name)} - [{self.mod_name}]" for name in names}
        return [self.root / folder for folder in folders]

    def saved_zips(self, entry):
        """Zips in the game's folder under the download root. The folder, unlike the file
        name, stays the same when the catalog moves to a new game version."""
        zips = []
        for folder in self.game_folders(entry):
            try:
                zips += [path for path in folder.iterdir() if path.suffix.lower() == ".zip" and path.is_file()]
            except OSError:
                pass
        return zips

    def status_of(self, entry):
        """What is in the download root decides; the record only says which release it is."""
        zips = self.saved_zips(entry)
        if not zips:
            return "missing"
        record = self.state.get(entry["id"])
        if not record:
            # Copied in by hand, so its hash is unknown: only the name can say which version it is.
            prefix = f"[{display_version(entry['game_version'])}-"
            return "ok" if any(path.name.startswith(prefix) for path in zips) else "update"
        # The original file's SHA-256 is the surest way to spot a new release. When the
        # catalog does not carry one, fall back to comparing the version it reports.
        if entry["sha256"] and record.get("sha256"):
            return "ok" if record["sha256"] == entry["sha256"] else "update"
        return "ok" if record.get("version", "") == entry["version"] else "update"

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
                "log": list(self.download_log),
                "batch": self.batch_progress(),
            }

    @staticmethod
    def new_batch():
        return {"count": 0, "finished": 0, "total": 0, "passed": 0, "saved": 0, "failed": 0, "cancelled": 0}

    def batch_progress(self):
        """How far the downloads queued since the app was last idle are, in bytes. Catalogs
        that give no sizes leave only the number of finished mods to go by."""
        with self.lock:
            batch = self.batch
            running = sum(job["done"] for job in self.jobs.values() if job["phase"] != "error")
            if batch["total"]:
                fraction = (batch["passed"] + running) / batch["total"]
            else:
                fraction = batch["finished"] / max(1, batch["count"])
            return {"active": self.busy(), "number": self.batch_number, "count": batch["count"],
                    "finished": batch["finished"],
                    "saved": batch["saved"], "failed": batch["failed"], "cancelled": batch["cancelled"],
                    "fraction": min(1, fraction)}

    def _finish_batch_item(self, entry, outcome="saved"):
        with self.lock:
            self.batch[outcome] += 1
            self.batch["finished"] += 1
            self.batch["passed"] += entry["size"]

    def enqueue(self, ids):
        with self.lock:
            if not self.busy():
                self.batch = self.new_batch()
                self.batch_number += 1
            known = {entry["id"]: entry for entry in self.items}
            for entry_id in ids:
                entry = known.get(entry_id)
                if entry and entry_id not in self.jobs:
                    self.batch["count"] += 1
                    self.batch["total"] += entry["size"]
                    self.jobs[entry_id] = {"phase": "queued", "done": 0, "total": entry["size"], "speed": 0}
                    self.cancels[entry_id] = threading.Event()
                    self.queue.put(entry)
                    self._log_game(entry, f"đã thêm vào hàng đợi")

    def cancel(self, entry_id):
        with self.lock:
            event = self.cancels.get(entry_id)
        if event:
            event.set()

    def cancel_all(self):
        """Stop every queued or running download. Partial files stay, so a later start resumes."""
        with self.lock:
            events = list(self.cancels.values())
        for event in events:
            event.set()
        if events:
            self._log_download(f"Dừng tất cả ({len(events)} mod)", "warn")

    def busy(self):
        with self.lock:
            return any(job["phase"] != "error" for job in self.jobs.values())

    def snapshot_jobs(self):
        with self.lock:
            return list(self.jobs.values())

    def _log_download(self, text, level="info"):
        with self.lock:
            self.download_log.append(log_line(text, level))

    def _log_game(self, entry, text, level="info"):
        self._log_download(f"{clean_game_name(entry['name'])}: {text}", level)

    def _update_job(self, entry_id, **fields):
        with self.lock:
            self.jobs[entry_id].update(fields)

    def _fetch(self, entry, url, part, cancel):
        """Download url into part, resuming when the server supports ranges."""
        size = entry["size"]  # 0 means the catalog did not say, so size checks are skipped
        have = part.stat().st_size if part.exists() else 0
        if size and have > size:
            part.unlink()
            have = 0
        if size and have == size:
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
                    if size and done > size:
                        raise ValueError("file lớn hơn kích thước trong catalog")
                    handle.write(block)
                    now = time.monotonic()
                    if now - mark >= 0.5:
                        self._update_job(entry["id"], phase="downloading", done=done,
                                         speed=(done - mark_done) / (now - mark))
                        mark, mark_done = now, done
                self._update_job(entry["id"], done=done)
            if size and done < size:
                raise OSError("kết nối bị ngắt giữa chừng")

    def _download(self, entry, cancel):
        """Return the path of a verified copy of the original file."""
        self.parts.mkdir(exist_ok=True)
        part = self.parts / f"{entry['id']}.{(entry['sha256'] or 'nohash')[:12]}.part"
        last_error = "không tải được"
        for url in entry["urls"]:
            for attempt in range(RETRIES):
                try:
                    self._fetch(entry, url, part, cancel)
                except Cancelled:
                    raise
                except urllib.error.HTTPError as error:
                    last_error = f"HTTP {error.code}"
                    self._log_game(entry, f"lỗi tải (lần {attempt + 1}): {last_error}", "warn")
                    if error.code in (403, 404, 410):
                        break
                except (OSError, ValueError, http.client.HTTPException) as error:
                    last_error = str(error) or type(error).__name__
                    self._log_game(entry, f"lỗi tải (lần {attempt + 1}): {last_error}", "warn")
                else:
                    # With no SHA-256 in the catalog there is nothing to verify against, so a
                    # completed download is accepted as-is.
                    if not entry["sha256"]:
                        self._log_game(entry, f"catalog không có SHA-256, bỏ qua kiểm tra", "warn")
                        return part
                    self._update_job(entry["id"], phase="verifying", speed=0)
                    self._log_game(entry, f"đang kiểm tra SHA-256")
                    if sha256_of(part) == entry["sha256"]:
                        return part
                    last_error = "SHA-256 không khớp"
                    self._log_game(entry, f"{last_error}", "error")
                    part.unlink(missing_ok=True)
                    break
                time.sleep(1 + attempt)
            if url != entry["urls"][-1]:
                self._log_game(entry, f"chuyển sang nguồn tải khác", "warn")
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
            # Only this game's own copy is replaced: a different mod name is a separate
            # folder that the user may still want.
            if old_path and old_path != target and old_path.parent in self.game_folders(entry):
                self._remove_replaced(old_path)

    def _remove_replaced(self, path):
        """Tidy up after a saved download. The new file is safe by now, and the user may have
        moved the old one away, so a failure here is not a failed download."""
        try:
            path.unlink(missing_ok=True)
            if path.parent != self.root and not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError:
            pass

    def worker(self):
        while True:
            entry = self.queue.get()
            entry_id = entry["id"]
            cancel = self.cancels[entry_id]
            target = self.root / normalized_path(entry["name"], entry["game_version"], self.mod_name)
            try:
                if cancel.is_set():
                    raise Cancelled
                self._update_job(entry_id, phase="downloading")
                self._log_game(entry, f"bắt đầu tải ({format_size(entry['size'])})" if entry["size"] else "bắt đầu tải")
                part = self._download(entry, cancel)
                self._update_job(entry_id, phase="repacking", speed=0)
                self._log_game(entry, f"đang đóng gói")
                repack(part, target, entry["title_id"], cancel)
                part.unlink()
                self._commit(entry, target)
                self._log_game(entry, f"đã lưu {target}", "ok")
                with self.lock:
                    del self.jobs[entry_id]
                    self._finish_batch_item(entry)
            except Cancelled:
                self._log_game(entry, "đã huỷ", "warn")
                with self.lock:
                    del self.jobs[entry_id]
                    self._finish_batch_item(entry, "cancelled")
            except Exception as error:
                self._log_game(entry, f"lỗi: {error}", "error")
                with self.lock:
                    self._update_job(entry_id, phase="error", message=str(error), speed=0)
                    self._finish_batch_item(entry, "failed")
            with self.lock:
                self.cancels.pop(entry_id, None)

    def known_titles(self):
        """Catalog entries by title ID, from every cached catalog, the active one first."""
        with self.lock:
            catalogs = sorted(self.catalogs, key=lambda catalog: catalog is not self.catalog)
        titles = {}
        for catalog in catalogs:
            try:
                raw = (self.catalogs_dir / f"{catalog['folder']}.json").read_bytes()
                items = parse_catalog(json.loads(raw), clean_mapping(catalog.get("mapping")))
            except (OSError, ValueError):
                continue
            for item in items:
                if item["title_id"]:
                    titles.setdefault(item["title_id"].upper(), item)
        return titles

    def title_names(self):
        """{title ID: game name}, from a cached database that is refreshed once a week. A
        failed refresh keeps the old copy, which may be empty on a first run offline."""
        path = self.catalogs_dir / TITLES_FILE
        names = self._load_json(path, {})
        try:
            if names and time.time() - path.stat().st_mtime < TITLES_MAX_AGE:
                return names
        except OSError:
            pass
        try:
            with http_get(TITLES_URL, timeout=60) as response:
                raw = response.read(MAX_TITLES + 1)
            if len(raw) > MAX_TITLES:
                raise ValueError("danh sách tên game quá lớn")
            fetched = parse_titles(json.loads(raw))
            if fetched:
                self._write_json(path, fetched)
                return fetched
        except (OSError, ValueError, http.client.HTTPException):
            pass
        return names

    def refresh_titles(self):
        names = self.title_names()
        with self.lock:
            if names != self.titles:
                self.titles = names
                self.items = self._named(self.items)
        return names

    def normalize_snapshot(self):
        with self.lock:
            job = dict(self.normalize_job)
            job["plans"] = [dict(plan) for plan in job["plans"]]
            job["log"] = list(job["log"])
            return job

    def normalizing(self):
        with self.lock:
            return self.normalize_job["phase"] in ("scanning", "working")

    def _set_normalize(self, **fields):
        with self.lock:
            self.normalize_job.update(fields)

    def _log(self, text, level="info"):
        with self.lock:
            self.normalize_job["log"].append(log_line(text, level))

    def scan_mods(self, source, dest):
        """Fill the normalize job with a plan for the user to review."""
        self._set_normalize(phase="scanning", plans=[], done=0, total=0, message="", failed=False, log=[])
        self._log(f"Quét {source.strip()}")
        try:
            if not source.strip() or not dest.strip():
                raise ValueError("Hãy nhập cả hai thư mục")
            mod = self.mod_name
            if not mod:
                raise ValueError("Hãy nhập tên mod")
            root = Path(source.strip()).expanduser().resolve()
            target = Path(dest.strip()).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"Không có thư mục: {root}")
            # Otherwise the scan would pick up its own results, or delete the folder it saves to.
            # The source may sit inside the destination: nothing is written into it.
            if root == target or root in target.parents:
                raise ValueError("Thư mục đích không được trùng hoặc nằm trong Thư mục nguồn")
            if target / BACKUP_DIR in (root, *root.parents):
                raise ValueError("Thư mục nguồn không được nằm trong thư mục backup")
            names = self.refresh_titles()
            plans = plan_mods(root, target, self.known_titles(), names, mod)
            ready = sum(plan["status"] == "ready" for plan in plans)
            message = f"Tìm thấy {len(plans)} mod, {ready} sẵn sàng chuẩn hóa." if plans else "Không tìm thấy mod nào."
            if plans and not names:
                message += " Không tải được danh sách tên game nên tên được đoán từ tên file."
            self._set_normalize(phase="scanned", plans=plans, root=root, dest=target, message=message)
            self._log(message, "ok" if ready and names else "warn")
        except (OSError, ValueError) as error:
            self._set_normalize(phase="idle", message=str(error) or type(error).__name__, failed=True)
            self._log(f"Lỗi: {self.normalize_job['message']}", "error")

    def _normalize_one(self, plan, cancel):
        def advance(count):
            with self.lock:
                plan["written"] += count
                self.normalize_job["bytes_done"] += count

        if plan["target"].exists():
            raise RuntimeError("Đã có file này ở thư mục đích")
        if plan["kind"] == "folder":
            repack_folder(plan["paths"], plan["target"], cancel, on_bytes=advance)
        else:
            repack(plan["source"], plan["target"], plan["title_id"], cancel, on_bytes=advance)

    def _finish_plan(self, plan, **fields):
        """Settle one mod. The overall bar counts the bytes of a failed mod as passed."""
        with self.lock:
            self.normalize_job["bytes_done"] += plan["size"] - plan["written"]
            plan.update(fields, written=plan["size"])

    def _back_up(self, path, root, backup, plans):
        try:
            if path.exists():
                target = back_up(path, root, backup)
                self._log(f"Đã chuyển vào backup: {target.relative_to(backup)}")
        except (OSError, shutil.Error) as error:
            self._log(f"Không chuyển được {path.relative_to(root)} vào backup: {error}", "error")
            with self.lock:
                for plan in plans:
                    plan["message"] = f"Đã lưu nhưng không chuyển được file gốc vào backup: {error}"

    def _back_up_sources(self, plans, root, backup):
        """Keep the originals of saved mods in the backup folder. A top-level source folder goes
        whole, with whatever else it holds, once every mod found in it was saved. Otherwise only
        the saved mods leave it, so anything failed or skipped keeps its files in place."""
        outer = {}
        for plan in plans:
            parts = plan["paths"][0].relative_to(root).parts
            outer.setdefault(root / parts[0] if len(parts) > 1 else plan["paths"][0], []).append(plan)
        for folder, group in outer.items():
            saved = [plan for plan in group if plan["status"] == "done"]
            if len(saved) == len(group):
                self._back_up(folder, root, backup, group)
            else:
                for plan in saved:
                    for path in plan["paths"]:
                        self._back_up(path, root, backup, [plan])

    def normalize_mods(self):
        """Run the reviewed plan. A source moves to the backup folder only once its zip is saved."""
        with self.lock:
            job = self.normalize_job
            plans, root, backup = job["plans"], job.get("root"), job["dest"] / BACKUP_DIR
            ready = [plan for plan in plans if plan["status"] == "ready"]
            job.update(phase="working", done=0, total=len(ready), message="", bytes_done=0,
                       bytes_total=sum(plan["size"] for plan in ready))
        cancel = self.normalize_cancel
        cancel.clear()
        self._log(f"Bắt đầu chuẩn hóa {len(ready)} mod")
        for plan in plans:
            if plan["status"] in ("conflict", "error"):
                self._log(f"Bỏ qua {clean_game_name(plan['name'])}: {plan['message']}",
                          "error" if plan["status"] == "error" else "warn")
        cancelled = False
        for plan in ready:
            try:
                if cancel.is_set():
                    raise Cancelled
                with self.lock:
                    plan["status"] = "working"
                self._log(f"Đang đóng gói {clean_game_name(plan['name'])} ({format_size(plan['size'])})")
                self._normalize_one(plan, cancel)
                self._finish_plan(plan, status="done")
                with self.lock:
                    job["done"] += 1
                self._log(f"Đã lưu {plan['target']}", "ok")
            except Cancelled:
                with self.lock:
                    job["bytes_done"] -= plan["written"]
                    plan.update(status="ready", written=0)
                cancelled = True
                break
            except Exception as error:
                self._finish_plan(plan, status="error", message=str(error) or type(error).__name__)
                self._log(f"Lỗi {clean_game_name(plan['name'])}: {plan['message']}", "error")
        self._back_up_sources(plans, root, backup)
        failed = sum(plan["status"] == "error" for plan in ready)
        message = f"Đã chuẩn hóa {job['done']}/{len(ready)} mod."
        message += f" {failed} mod lỗi." if failed else ""
        message = "Đã huỷ. " + message if cancelled else message
        self._log(message, "error" if failed else "warn" if cancelled else "ok")
        self._set_normalize(phase="scanned", message=message, runs=job["runs"] + 1)

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
            speed = f" · {format_size(job['speed'])}/s" if job["speed"] else ""
            if job["total"]:
                return f"{100 * job['done'] / job['total']:.0f}%{speed}"
            return f"{format_size(job['done'])}{speed}"  # size unknown: show bytes pulled so far
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


def popup(title, message, parent, buttons):
    """A modal dialog with centered text. Native message boxes cannot center it. Returns the
    index of the chosen button; closing the window counts as the last one."""
    window = tk.Toplevel(parent, bg=COLORS["bg"])
    window.withdraw()
    window.title(title)
    window.resizable(False, False)
    window.transient(parent.winfo_toplevel())
    chosen = [len(buttons) - 1]

    def choose(index):
        chosen[0] = index
        window.destroy()

    frame = ttk.Frame(window, padding=(28, 22, 28, 18))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=message, justify="center", anchor="center", wraplength=460).pack(fill="x", pady=(0, 18))
    row = ttk.Frame(frame)
    row.pack()
    for index, label in enumerate(buttons):
        ttk.Button(row, text=label, width=10, style="TButton" if index else "Accent.TButton",
                   command=lambda value=index: choose(value)).pack(side="left", padx=4)
    window.bind("<Return>", lambda _: choose(0))
    window.bind("<Escape>", lambda _: choose(len(buttons) - 1))
    window.protocol("WM_DELETE_WINDOW", lambda: choose(len(buttons) - 1))
    parent.update_idletasks()
    window.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 2
    window.geometry(f"+{max(0, x)}+{max(0, y)}")
    window.deiconify()
    window.wait_visibility()
    window.grab_set()
    window.focus_set()
    window.wait_window()
    return chosen[0]


def show_message(title, message, parent):
    popup(title, message, parent, ("OK",))


def ask_yes_no(title, message, parent):
    return popup(title, message, parent, ("Có", "Không")) == 0


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
        widget.bind("<Control-Button-1>", lambda _: self.app.toggle_pick(self.id))
        widget.bind("<Shift-Button-1>", lambda _: self.app.pick_range(self.id))
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
        values = (clean_game_name(item["name"]), item["version"] or "-", "Miss" if missing else version,
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
        self._maximize()
        self.root.configure(bg=COLORS["bg"])
        self.items = {}
        self.shown_ids = []
        self.rows = {}
        self.row_signature = None
        self.selected = None
        self.picked = set()
        self.pick_anchor = None
        self.chosen = []
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
        self.prompted_tag = None
        self.reported_batch = 0
        self.catalog_folders = []
        self.manager = None
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _maximize(self):
        try:
            self.root.state("zoomed")  # Windows and macOS
        except tk.TclError:
            self.root.attributes("-zoomed", True)  # X11

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
        style.configure("Horizontal.TScrollbar", background=c["panel"], arrowcolor=c["muted"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(14, 6), borderwidth=0,
                        bordercolor=c["bg"], lightcolor=c["bg"], darkcolor=c["bg"])
        # Tabs show where you are by color alone, so they drop the focus ring and the border.
        style.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
        button_layout = [("Button.border", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]
        style.layout("View.TButton", button_layout)
        style.layout("ViewActive.Accent.TButton", button_layout)
        # clam enlarges the selected tab through these two options; keep every tab the same size.
        style.map("TNotebook.Tab", background=[("selected", c["accent"]), ("active", c["line"])],
                  foreground=[("selected", c["accent_ink"])], padding=[("selected", (14, 6))],
                  expand=[("selected", (0, 0, 0, 0))],
                  bordercolor=[("selected", c["accent"]), ("active", c["line"])],
                  lightcolor=[("selected", c["accent"]), ("active", c["line"])],
                  darkcolor=[("selected", c["accent"]), ("active", c["line"])])

    def _build(self):
        header = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="PatchVault", style="Title.TLabel").pack(side="left")
        self.summary = ttk.Label(header, style="Muted.TLabel")
        self.summary.pack(side="left", padx=14)
        self.mod_name = tk.StringVar(value=self.vault.mod_name)
        self.mod_entry = ttk.Entry(header, textvariable=self.mod_name, width=24)
        self.mod_entry.pack(side="right")
        ttk.Label(header, text="Tên mod", style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self.mod_name.trace_add("write", lambda *_: self.vault.set_mod_name(self.mod_name.get()))
        self.tabs = ttk.Notebook(self.root, takefocus=False)
        self.tabs.pack(fill="both", expand=True, padx=14)
        library = ttk.Frame(self.tabs)
        self.tabs.add(library, text="Tải về")
        normalizer = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(normalizer, text="Chuẩn hóa")
        self.normalizer = NormalizeTab(self, normalizer)
        top = ttk.Frame(library, padding=(0, 10, 0, 6))
        top.pack(fill="x")
        target = self.library_target = ttk.Frame(library, padding=(0, 0, 0, 8))
        target.pack(fill="x")
        self.overall = ttk.Frame(library, padding=(0, 0, 0, 8))
        self.overall_label = ttk.Label(self.overall, style="Muted.TLabel")
        self.overall_label.pack(side="left", padx=(0, 10))
        self.overall_bar = ttk.Progressbar(self.overall, maximum=1000)
        self.overall_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(target, text="Thư mục đích", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.library_views = ViewSwitch(target, self.show_library_view)
        self.download_button = ttk.Button(target, text="Chọn…", command=self.browse_download)
        self.download_button.pack(side="right", padx=(6, 0))
        self.download_dir = tk.StringVar(value=self.vault.download_dir)
        self.download_entry = ttk.Entry(target, textvariable=self.download_dir)
        self.download_entry.pack(side="left", fill="x", expand=True)
        self.download_dir.trace_add("write", lambda *_: self.vault.set_download_dir(self.download_dir.get()))
        self.open_button = ttk.Button(top, text="Mở thư mục", command=self.open_folder)
        self.open_button.pack(side="right", padx=(6, 0))
        self.check_button = ttk.Button(top, text="Cập nhật catalog", command=self.check)
        self.check_button.pack(side="right", padx=(6, 0))
        self.get_all_button = ttk.Button(top, command=self.get_all)
        self.get_all_button.pack(side="right", padx=(6, 0))
        self.update_all_button = ttk.Button(top, style="Accent.TButton", command=self.update_all)
        self.chosen_button = ttk.Button(top, style="Accent.TButton", command=self.get_chosen)
        self.stop_button = ttk.Button(top, text="Dừng tất cả", command=self.vault.cancel_all)
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

        body = self.library_body = ttk.Frame(library)
        self.download_log = LogPane(self, library)
        self.library_views.show("list")
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
        self.list_canvas.bind("<Return>", lambda _: self.get_chosen() if len(self.picked) > 1 else self.act())
        self.root.bind("<Control-a>", self.pick_all)
        self.root.bind("<Control-A>", self.pick_all)

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
        self.app_update_button = ttk.Button(foot, style="Accent.TButton", command=self.start_app_update)

    def show_library_view(self, view):
        shown, hidden = (self.download_log.frame, self.library_body) if view == "log" \
            else (self.library_body, self.download_log.frame)
        hidden.pack_forget()
        shown.pack(fill="both", expand=True)

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
        haystack = " ".join([item["name"], clean_game_name(item["name"]), item["developer"], item["translator"],
                             *item["tags"]]).lower()
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
        self.picked = {entry_id}
        self.pick_anchor = entry_id
        self.show_picked(entry_id)

    def show_picked(self, entry_id):
        self.selected = entry_id
        self.list_canvas.focus_set()
        self.paint_rows()
        self.show_detail()
        self.paint_chosen()

    def toggle_pick(self, entry_id):
        if entry_id in self.picked and len(self.picked) > 1:
            self.picked.discard(entry_id)
            entry_id = next(shown for shown in self.shown_ids if shown in self.picked)
        else:
            self.picked.add(entry_id)
        self.pick_anchor = entry_id
        self.show_picked(entry_id)

    def pick_range(self, entry_id):
        if self.pick_anchor not in self.shown_ids:
            self.select(entry_id)
            return
        first, last = sorted((self.shown_ids.index(self.pick_anchor), self.shown_ids.index(entry_id)))
        self.picked = set(self.shown_ids[first:last + 1])
        self.show_picked(entry_id)

    def pick_all(self, _event):
        """Ctrl+A for the game list. Typing fields keep their own meaning of the shortcut."""
        if self.tabs.index("current") == 0 and self.shown_ids \
                and not isinstance(self.root.focus_get(), (tk.Entry, tk.Text)):
            self.picked = set(self.shown_ids)
            self.show_picked(self.selected if self.selected in self.picked else self.shown_ids[0])
        return "break"

    def paint_chosen(self):
        """Offer the picked games that can be downloaded, once more than one is picked."""
        items = (self.items.get(entry_id) for entry_id in self.shown_ids or [] if entry_id in self.picked)
        self.chosen = [item["id"] for item in items
                       if item and not item["job"] and item["status"] in ("missing", "update")]
        if len(self.picked) > 1:
            self.chosen_button.configure(text=f"Tải đã chọn ({len(self.chosen)})")
            self.chosen_button.state(["!disabled"] if self.chosen else ["disabled"])
            if not self.chosen_button.winfo_manager():
                self.chosen_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.chosen_button.pack_forget()

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
            self.picked = (self.picked & set(ids)) or ({self.selected} if ids else set())
            self.pick_anchor = self.selected
            self.list_canvas.yview_moveto(0)
        self.paint_rows()

    def paint_rows(self):
        for entry_id in self.shown_ids or []:
            self.rows[entry_id].paint(self.items[entry_id], entry_id in self.picked)

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
        self.name.configure(text=clean_game_name(item["name"]))
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
            self.progress["value"] = 1000 * job["done"] / job["total"] if job["total"] else 0
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
        self.download_log.paint(snapshot["log"])
        self.paint_overall(snapshot["batch"])
        self.normalizer.paint(self.vault.normalize_snapshot())
        self.mod_entry.state(["disabled"] if self.vault.normalizing() else ["!disabled"])
        downloading = ["disabled"] if self.vault.busy() else ["!disabled"]
        self.download_entry.state(downloading)
        self.download_button.state(downloading)
        self.paint_app_update(snapshot["app_update"], snapshot["app_update_job"])
        self.root.after(400, self.refresh)

    def paint_overall(self, batch):
        if not batch["active"]:
            self.overall.pack_forget()
            # Numbers, not a seen-it-running flag: a short batch can start and end between repaints.
            if batch["number"] != self.reported_batch:
                self.reported_batch = batch["number"]
                self.report_downloads(batch)
            return
        self.overall_bar["value"] = 1000 * batch["fraction"]
        self.overall_label.configure(
            text=f"Đang tải {batch['finished']}/{batch['count']} mod · {100 * batch['fraction']:.0f}%")
        if not self.overall.winfo_manager():
            self.overall.pack(fill="x", after=self.library_target)

    def report_downloads(self, batch):
        """Say how a batch ended, unless the user stopped it before anything came of it."""
        if batch["saved"] or batch["failed"]:
            text = f"Đã tải xong {batch['saved']}/{batch['count']} mod."
            if batch["failed"]:
                text += f" {batch['failed']} mod lỗi."
            if batch["cancelled"]:
                text += f" {batch['cancelled']} mod đã huỷ."
            self.root.after_idle(show_message, "Tải xong", text, self.root)

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
            show_message("Đang tải", "Hãy đợi các file đang tải xong rồi đổi catalog.", parent=self.root)
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
        self.paint_chosen()
        if self.vault.busy():
            if not self.stop_button.winfo_manager():
                self.stop_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.stop_button.pack_forget()
        self.check_button.configure(text="Đang cập nhật…" if snapshot["checking"] else "Cập nhật catalog")
        self.check_button.state(["disabled"] if snapshot["checking"] else ["!disabled"])
        if not self.vault.catalog:
            self.summary.configure(text="Chưa có catalog")
            self.footer.configure(text="Bấm Quản lý để thêm catalog đầu tiên", style="Muted.TLabel")
        elif snapshot["error"]:
            self.footer.configure(text="Không cập nhật được catalog: " + snapshot["error"], style="Error.TLabel")
        else:
            self.footer.configure(text="Lưu vào: " + snapshot["dir"], style="Muted.TLabel")

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
            self.app_update_button.pack(side="right", padx=(0, 10))
        if update["tag"] != self.prompted_tag:
            self.prompted_tag = update["tag"]
            self.root.after_idle(self.prompt_app_update)

    def prompt_app_update(self):
        action = "Tải về và cập nhật ngay? Ứng dụng sẽ tự khởi động lại." if can_self_update(self.app_update) \
            else "Mở trang tải về để cập nhật?"
        if ask_yes_no("Có phiên bản mới", f"Đã có PatchVault {self.app_update['tag']}. {action}",
                               parent=self.root):
            self.start_app_update()

    def start_app_update(self):
        update = self.app_update
        if not can_self_update(update):
            open_in_system(update["url"])
        elif self.vault.normalizing():
            show_message("Đang chuẩn hóa", "Hãy đợi chuẩn hóa xong rồi cập nhật ứng dụng.", parent=self.root)
        elif any(job["phase"] != "error" for job in self.vault.snapshot_jobs()):
            show_message("Đang tải", "Hãy đợi các file đang tải xong rồi cập nhật ứng dụng.", parent=self.root)
        else:
            self.vault.set_update_job(phase="queued", done=0, total=1)
            threading.Thread(target=self.vault.install_app_update, args=(update,), daemon=True).start()

    def report_update_error(self, message):
        text = f"Không cập nhật được: {message}\n\nMở trang tải về để cập nhật thủ công?"
        if ask_yes_no("Lỗi cập nhật", text, parent=self.root):
            open_in_system(self.app_update["url"])

    def has_mod_name(self):
        """Every saved zip carries the mod name, so nothing can start without one."""
        if self.vault.mod_name:
            return True
        show_message("Thiếu tên mod", "Hãy nhập Tên mod ở góc trên bên phải trước khi tải.", parent=self.root)
        self.mod_entry.focus_set()
        return False

    def act(self):
        item = self.items.get(self.detail_id)
        if not item:
            return
        job = item["job"]
        if job and job["phase"] != "error":
            self.vault.cancel(item["id"])
        elif self.has_mod_name():
            self.vault.clear_error(item["id"])
            self.vault.enqueue([item["id"]])

    def update_all(self):
        if self.has_mod_name():
            self.vault.enqueue(self.updates)

    def get_chosen(self):
        if self.chosen and self.has_mod_name():
            self.vault.enqueue(self.chosen)

    def get_all(self):
        if self.has_mod_name():
            self.vault.enqueue(self.missing)

    def check(self):
        threading.Thread(target=self.vault.check, daemon=True).start()

    def browse_download(self):
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.download_dir.get() or None)
        if folder:
            self.download_dir.set(folder)

    def open_folder(self):
        self.vault.root.mkdir(parents=True, exist_ok=True)
        open_in_system(self.vault.root)

    def close(self):
        active = [job for job in self.vault.snapshot_jobs() if job["phase"] != "error"]
        if self.vault.app_update_job:
            active.append(self.vault.app_update_job)
        if active and not ask_yes_no(
                "Đang tải", "Vẫn còn file đang tải. Thoát bây giờ? Lần sau có thể tải tiếp từ phần đã tải.",
                parent=self.root):
            return
        if self.vault.normalizing() and not ask_yes_no(
                "Đang chuẩn hóa", "Vẫn đang chuẩn hóa mod. Thoát bây giờ? File gốc chưa xử lý xong vẫn được giữ.",
                parent=self.root):
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
        self.window.geometry("640x430")
        self.window.minsize(520, 360)
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

        # Advanced panel: an optional map from PatchVault's fields to this catalog's own keys,
        # for a JSON shape the heuristics do not recognise. Blank = pure auto-detect.
        self.advanced = tk.BooleanVar(value=False)
        self.toggle = tk.Checkbutton(frame, variable=self.advanced,
                                     command=self.toggle_advanced, bg=COLORS["bg"], fg=COLORS["muted"],
                                     activebackground=COLORS["bg"], activeforeground=COLORS["ink"],
                                     selectcolor=COLORS["field"], bd=0, highlightthickness=0,
                                     font=app.font, anchor="w", text="Ánh xạ trường (nâng cao)")
        self.toggle.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.fields = {}
        self.panel = ttk.Frame(frame)
        self.panel.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.panel.columnconfigure(1, weight=1)
        self.panel.columnconfigure(3, weight=1)
        ttk.Label(self.panel, wraplength=560, justify="left", style="Muted.TLabel",
                  text="Để trống = tự dò. Điền tên khoá trong JSON của catalog; có thể dùng dấu "
                       "chấm để vào mảng con, ví dụ files.0.url.").grid(row=0, column=0, columnspan=4,
                                                                        sticky="w", pady=(0, 6))
        for index, (field, label) in enumerate(MAPPABLE_FIELDS):
            var = tk.StringVar()
            self.fields[field] = var
            row, column = 1 + index // 2, (index % 2) * 2
            ttk.Label(self.panel, text=label, style="Muted.TLabel").grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=2)
            ttk.Entry(self.panel, textvariable=var, width=16).grid(
                row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=2)
        self.panel.grid_remove()

        self.status = ttk.Label(frame, style="Muted.TLabel", wraplength=560, justify="left")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_button = ttk.Button(frame, text="Thêm catalog", style="Accent.TButton", command=self.add)
        self.add_button.grid(row=7, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.fill_list()
        url_entry.focus_set()

    def toggle_advanced(self):
        if self.advanced.get():
            self.panel.grid()
        else:
            self.panel.grid_remove()

    def current_mapping(self):
        return {field: var.get() for field, var in self.fields.items() if var.get().strip()}

    def fill_list(self):
        self.listbox.delete(0, "end")
        for catalog in self.vault.catalogs:
            marker = "  ⚙" if catalog.get("mapping") else ""
            self.listbox.insert("end", f"{catalog['name']}  ·  {catalog['url']}{marker}")
        self.paint_buttons()

    def paint_buttons(self):
        self.remove_button.state(["!disabled"] if self.listbox.curselection() else ["disabled"])

    def add(self):
        if self.result is not None or "disabled" in self.add_button.state():
            return
        self.add_button.state(["disabled"])
        self.status.configure(text="Đang đọc catalog…", style="Muted.TLabel")
        threading.Thread(target=self.run_add, args=(self.name.get(), self.url.get(), self.current_mapping()),
                         daemon=True).start()
        self.window.after(150, self.wait_for_add)

    def run_add(self, name, url, mapping):
        try:
            catalog = self.vault.add_catalog(name, url, mapping)
            self.result = ("ok", f"Đã thêm {catalog['name']} ({len(self.vault.items)} game).")
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
            for var in self.fields.values():
                var.set("")
            self.fill_list()

    def remove(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        catalog = self.vault.catalogs[selected[0]]
        if self.vault.busy() and catalog is self.vault.catalog:
            self.status.configure(text="Hãy đợi các file đang tải xong.", style="Error.TLabel")
            return
        if ask_yes_no("Xoá catalog", f"Xoá {catalog['name']} khỏi danh sách? File đã tải vẫn được giữ lại.",
                               parent=self.window):
            self.vault.remove_catalog(catalog["folder"])
            self.fill_list()


class LogPane:
    """Read-only log that only appends what is new and follows the end while scrolled there."""

    FONT_SIZE = 12

    def __init__(self, app, parent):
        self.frame = ttk.Frame(parent)
        self.lines = 0
        self.text = tk.Text(self.frame, wrap="word", bd=0, highlightthickness=0, padx=8, pady=6, spacing1=2,
                            font=(app.family, self.FONT_SIZE), bg=COLORS["field"], fg=COLORS["ink"], state="disabled",
                            selectbackground=COLORS["select"], inactiveselectbackground=COLORS["select"])
        self.text.tag_configure("info", foreground=COLORS["muted"])
        self.text.tag_configure("ok", foreground=COLORS["ok"])
        self.text.tag_configure("warn", foreground=COLORS["accent"])
        self.text.tag_configure("error", foreground=COLORS["bad"], font=(app.family, self.FONT_SIZE, "bold"))
        scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

    def paint(self, lines):
        if len(lines) == self.lines:
            return
        self.text.configure(state="normal")
        if len(lines) < self.lines:
            self.text.delete("1.0", "end")
            self.lines = 0
        following = self.text.yview()[1] >= 1
        for level, line in lines[self.lines:]:
            self.text.insert("end", line + "\n", level)
        self.lines = len(lines)
        self.text.configure(state="disabled")
        if following:
            self.text.see("end")


class ViewSwitch:
    """Danh sách and Log buttons at the right of a toolbar. swap(view) shows that view."""

    def __init__(self, toolbar, swap):
        self.swap = swap
        self.log_tab = ttk.Button(toolbar, text="Log", takefocus=False, command=lambda: self.show("log"))
        self.log_tab.pack(side="right")
        self.list_tab = ttk.Button(toolbar, text="Danh sách", takefocus=False, command=lambda: self.show("list"))
        self.list_tab.pack(side="right", padx=(12, 6))

    def show(self, view):
        self.swap(view)
        self.log_tab.configure(style="ViewActive.Accent.TButton" if view == "log" else "View.TButton")
        self.list_tab.configure(style="View.TButton" if view == "log" else "ViewActive.Accent.TButton")


class ModRow:
    """One row of the normalize list. Like GameRow, it exists because a Treeview cell cannot
    wrap long text or hold a real progress bar."""

    COLORS = {"ready": "ink", "working": "accent", "done": "ok", "conflict": "muted", "error": "bad"}

    def __init__(self, tab):
        self.tab = tab
        self.painted = None
        self.frame = tk.Frame(tab.rows_frame, bg=COLORS["field"])
        self.labels = {}
        for index, (name, _, _) in enumerate(tab.COLUMNS):
            if name == "progress":
                self.progress_cell = tk.Frame(self.frame, bg=COLORS["field"], padx=8, pady=9)
                self.progress_cell.grid(row=0, column=index, sticky="new")
                self.bar = ttk.Progressbar(self.progress_cell, maximum=1000)
                self.percent = tk.Label(self.progress_cell, width=5, anchor="e", bd=0, font=tab.app.font,
                                        bg=COLORS["field"], fg=COLORS["ink"])
                self.percent.pack(side="right")
                self.bar.pack(side="left", fill="x", expand=True)
            else:
                label = tk.Label(self.frame, anchor="nw", justify="left", padx=8, pady=8, bd=0,
                                 font=tab.app.font, bg=COLORS["field"], fg=COLORS["ink"])
                label.grid(row=0, column=index, sticky="nsew")
                self.labels[name] = label
        for widget in (self.frame, self.progress_cell, self.bar, self.percent, *self.labels.values()):
            tab.bind_scroll(widget)

    def fit(self, widths):
        for index, ((name, _, _), width) in enumerate(zip(self.tab.COLUMNS, widths)):
            self.frame.columnconfigure(index, minsize=width)
            if name in self.labels and name not in self.tab.FITTED:
                self.labels[name].configure(wraplength=width - 16)

    def paint(self, cells, status, fraction):
        key = (cells, status, fraction)
        if key == self.painted:
            return
        self.painted = key
        color = COLORS[self.COLORS[status]]
        for name, label in self.labels.items():
            label.configure(text=cells[name], fg=color)
        if fraction is None:
            self.bar.pack_forget()
            self.percent.configure(text="-", fg=color)
        else:
            if not self.bar.winfo_manager():
                self.bar.pack(side="left", fill="x", expand=True)
            self.bar["value"] = 1000 * fraction
            self.percent.configure(text=f"{100 * fraction:.0f}%", fg=color)


class NormalizeTab:
    """Tab that tidies a folder of mods into the vault layout: scan, review, then move."""

    # Width in pixels, or a share of what the fixed columns leave. Columns in FITTED grow to
    # their content and never wrap; when they need more room, the table scrolls sideways.
    COLUMNS = (("source", "Nguồn", 0.4), ("title", "Title ID", 200), ("name", "Tên game", 0.25), ("mod", "Tên mod", 140),
               ("version", "Phiên bản game", 125), ("size", "Dung lượng", 100), ("progress", "Tiến độ", 200),
               ("output", "Tên sau chuẩn hóa", 300), ("status", "Trạng thái", 0.35))
    FITTED = ("version", "output")
    MIN_SHARED = 760
    CELL_PADDING = 18
    STATUS = {"ready": "Sẵn sàng", "working": "Đang đóng gói", "done": "Đã chuẩn hóa", "conflict": "Bỏ qua",
              "error": "Lỗi"}

    def __init__(self, app, parent):
        self.app = app
        self.vault = app.vault
        self.painted = None
        self.row_keys = []
        self.reported_runs = 0
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(4, weight=1)
        self.source = tk.StringVar()
        self.dest = tk.StringVar(value=self.vault.normalize_dest)
        self.dest.trace_add("write", lambda *_: self.vault.set_normalize_dest(self.dest.get()))
        for row, (label, variable) in enumerate((("Thư mục nguồn", self.source), ("Thư mục đích", self.dest))):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(parent, text="Chọn…", command=lambda v=variable: self.browse(v)).grid(
                row=row, column=2, padx=(6, 0), pady=3)
        self.dest_entry = parent.grid_slaves(row=1, column=1)[0]
        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        self.scan_button = ttk.Button(actions, text="Quét", command=self.scan)
        self.scan_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Chuẩn hóa", style="Accent.TButton", command=self.run)
        self.run_button.pack(side="left", padx=(6, 0))
        self.cancel_button = ttk.Button(actions, text="Huỷ", command=self.vault.normalize_cancel.set)
        self.status = ttk.Label(actions, style="Muted.TLabel")
        self.status.pack(side="left", padx=(12, 0))
        self.views = ViewSwitch(actions, self.show_view)
        self.progress = ttk.Progressbar(parent, maximum=1000)
        self.table = tk.Frame(parent, bg=COLORS["panel"])
        self.header_canvas = tk.Canvas(self.table, bg=COLORS["panel"], bd=0, highlightthickness=0, height=1)
        self.header_canvas.pack(fill="x")
        self.header = tk.Frame(self.header_canvas, bg=COLORS["panel"])
        self.header_window = self.header_canvas.create_window(0, 0, anchor="nw", window=self.header)
        self.header.bind("<Configure>", lambda event: self.header_canvas.configure(
            height=event.height, scrollregion=self.header_canvas.bbox("all")))
        for index, (_, title, _) in enumerate(self.COLUMNS):
            tk.Label(self.header, text=title, anchor="w", padx=8, pady=7, bd=0, font=app.font,
                     bg=COLORS["panel"], fg=COLORS["muted"]).grid(row=0, column=index, sticky="ew")
        self.holder = tk.Frame(self.table, bg=COLORS["field"])
        self.holder.pack(fill="both", expand=True)
        holder = self.holder
        self.canvas = tk.Canvas(holder, bg=COLORS["field"], bd=0, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        # The header stops where the rows do, or the two would scroll by different amounts.
        self.header_canvas.pack_configure(padx=(0, scroll.winfo_reqwidth()))
        self.side_scroll = ttk.Scrollbar(self.table, orient="horizontal", command=self.scroll_sideways)
        self.canvas.configure(yscrollcommand=scroll.set, xscrollcommand=self.follow_sideways)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows_frame = tk.Frame(self.canvas, bg=COLORS["line"])
        self.rows_window = self.canvas.create_window(0, 0, anchor="nw", window=self.rows_frame)
        self.rows_frame.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.fit)
        self.bind_scroll(self.canvas)
        self.rows = []
        self.widths = [width if width > 1 else 200 for _, _, width in self.COLUMNS]
        self.working = None
        self.measure = tkfont.Font(root=app.root, font=app.font)
        self.canvas_width = 0
        self.fitted = {name: self.measure.measure(title) + self.CELL_PADDING
                       for name, title, _ in self.COLUMNS if name in self.FITTED}
        self.log = LogPane(app, parent)
        self.views.show("list")

    def bind_scroll(self, widget):
        widget.bind("<Button-4>", lambda _: self.scroll(-1))
        widget.bind("<Button-5>", lambda _: self.scroll(1))
        widget.bind("<MouseWheel>", lambda event: self.scroll(-1 if event.delta > 0 else 1))
        widget.bind("<Shift-Button-4>", lambda _: self.scroll_sideways("scroll", -3, "units"))
        widget.bind("<Shift-Button-5>", lambda _: self.scroll_sideways("scroll", 3, "units"))
        widget.bind("<Shift-MouseWheel>", lambda event: self.scroll_sideways(
            "scroll", -3 if event.delta > 0 else 3, "units"))

    def scroll_sideways(self, *args):
        self.canvas.xview(*args)

    def follow_sideways(self, first, last):
        """Keep the header over its columns, and show the bar only when there is more to see."""
        self.side_scroll.set(first, last)
        self.header_canvas.xview_moveto(first)
        if float(last) - float(first) >= 1:
            self.side_scroll.pack_forget()
        elif not self.side_scroll.winfo_manager():
            self.side_scroll.pack(side="bottom", fill="x", before=self.holder)

    def scroll(self, units):
        if self.rows_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(units, "units")

    def fit(self, event):
        self.canvas_width = event.width
        self.apply_widths()

    def apply_widths(self):
        widths = [self.fitted.get(name, width) for name, _, width in self.COLUMNS]
        spare = max(self.MIN_SHARED, self.canvas_width - sum(width for width in widths if width > 1))
        self.widths = [width if width > 1 else int(spare * width) for width in widths]
        total = max(self.canvas_width, sum(self.widths))
        self.canvas.itemconfigure(self.rows_window, width=total)
        self.header_canvas.itemconfigure(self.header_window, width=total)
        for index, width in enumerate(self.widths):
            self.header.columnconfigure(index, minsize=width)
        for row in self.rows:
            row.fit(self.widths)

    def show_view(self, view):
        shown, hidden = (self.log.frame, self.table) if view == "log" else (self.table, self.log.frame)
        hidden.grid_remove()
        shown.grid(row=4, column=0, columnspan=3, sticky="nsew")

    def browse(self, variable):
        folder = filedialog.askdirectory(parent=self.app.root, initialdir=variable.get() or None)
        if folder:
            variable.set(folder)

    def scan(self):
        if not self.vault.normalizing():
            threading.Thread(target=self.vault.scan_mods, args=(self.source.get(), self.dest.get()),
                             daemon=True).start()

    def run(self):
        count = sum(plan["status"] == "ready" for plan in self.vault.normalize_snapshot()["plans"])
        if count and not self.vault.normalizing() and ask_yes_no(
                "Chuẩn hóa mod", f"Chuẩn hóa {count} mod? Sau khi lưu xong, file gốc và cả thư mục ngoài cùng chứa nó "
                f"trong Thư mục nguồn sẽ được chuyển vào thư mục {BACKUP_DIR} trong Thư mục đích.",
                parent=self.app.root):
            threading.Thread(target=self.vault.normalize_mods, daemon=True).start()

    def cells(self, plan, root):
        source = plan["source"]
        shown = source.relative_to(root).as_posix() if root else source.name
        status = self.STATUS[plan["status"]]
        if plan["message"]:
            status += ": " + plan["message"]
        title = plan["title_id"] or "-"
        if plan["extra_titles"] > 0:
            title += f" +{plan['extra_titles']}"
        version = display_version(plan["version"])
        return {"source": shown, "title": title, "name": clean_game_name(plan["name"]), "mod": plan["mod"],
                "version": "Miss" if version == MISSING_VERSION else (
                    version if plan["internal"] == INTERNAL_VERSION else f"{version}-{plan['internal']}"),
                "status": status,
                "size": format_size(plan["stored"]) if plan["stored"] else "-",
                "output": "-" if plan["status"] == "error" else
                f"{plan['target'].parent.name}/{plan['target'].name}"}

    @staticmethod
    def fraction(plan):
        """How far a mod is, or None when it will not be packed."""
        if plan["status"] not in ("ready", "working", "done"):
            return None
        return 1 if plan["status"] == "done" else min(1, plan["written"] / max(1, plan["size"]))

    def paint_rows(self, plans, root):
        keys = [(plan["source"], plan["title_id"]) for plan in plans]
        if keys != self.row_keys:
            self.row_keys = keys
            for row in self.rows:
                row.frame.destroy()
            self.rows = [ModRow(self) for _ in plans]
            for row in self.rows:
                row.fit(self.widths)
                row.frame.pack(fill="x", pady=(0, 1))
            self.canvas.yview_moveto(0)
        cells = [self.cells(plan, root) for plan in plans]
        wanted = {name: max([self.measure.measure(title), *(self.measure.measure(cell[name]) for cell in cells)])
                  + self.CELL_PADDING for name, title, _ in self.COLUMNS if name in self.FITTED}
        if wanted != self.fitted:
            self.fitted = wanted
            self.apply_widths()
        for row, plan, cell in zip(self.rows, plans, cells):
            row.paint(cell, plan["status"], self.fraction(plan))
        working = next((index for index, plan in enumerate(plans) if plan["status"] == "working"), None)
        if working != self.working:
            self.working = working
            if working is not None:
                self.see(self.rows[working].frame)

    def see(self, frame):
        self.app.root.update_idletasks()
        total = self.rows_frame.winfo_height()
        visible = self.canvas.winfo_height()
        top = self.canvas.canvasy(0)
        if frame.winfo_y() < top:
            self.canvas.yview_moveto(frame.winfo_y() / total)
        elif frame.winfo_y() + frame.winfo_height() > top + visible:
            self.canvas.yview_moveto((frame.winfo_y() + frame.winfo_height() - visible) / total)

    def paint(self, job):
        plans = job["plans"]
        key = (job["phase"], job["runs"], job["done"], job["message"], job["bytes_done"], len(job["log"]),
               *((plan["status"], plan["message"], plan["written"]) for plan in plans))
        if key == self.painted:
            return
        self.painted = key
        busy = job["phase"] in ("scanning", "working")
        ready = sum(plan["status"] == "ready" for plan in plans)
        self.scan_button.state(["disabled"] if busy else ["!disabled"])
        self.dest_entry.state(["disabled"] if busy else ["!disabled"])
        self.run_button.state(["!disabled"] if ready and not busy else ["disabled"])
        self.run_button.configure(text=f"Chuẩn hóa ({ready})" if ready else "Chuẩn hóa")
        if job["phase"] == "working":
            fraction = min(1, job["bytes_done"] / max(1, job["bytes_total"]))
            self.cancel_button.pack(side="left", padx=(6, 0), after=self.run_button)
            self.progress.configure(value=1000 * fraction)
            self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
            text = f"Đang chuẩn hóa {job['done']}/{job['total']} mod · {100 * fraction:.0f}%"
        else:
            self.cancel_button.pack_forget()
            self.progress.grid_remove()
            text = "Đang quét… Lần đầu cần tải danh sách tên game nên sẽ lâu hơn." if job["phase"] == "scanning" \
                else job["message"]
        self.status.configure(text=text, style="Error.TLabel" if job["failed"] else "Muted.TLabel")
        self.paint_rows(plans, job.get("root"))
        self.log.paint(job["log"])
        if job["runs"] != self.reported_runs:
            self.reported_runs = job["runs"]
            if job["done"] or any(plan["status"] == "error" for plan in plans):
                self.app.root.after_idle(show_message, "Chuẩn hóa xong", job["message"], self.app.root)


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
        vault.refresh_titles()
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
        old = vault.state.get(entry["id"], {}).get("version") or "?"
        print(f"Có cập nhật: {entry['name']} ({old} -> {entry['version'] or '?'})")
    print(f"{vault.catalog['name']}: {len(updates)} bản cập nhật, {missing} game chưa tải, "
          f"tổng {len(vault.items)} game.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Tải, theo dõi và đóng gói lại mod game.")
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
