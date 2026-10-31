import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
MAX_UNPACKED = 32 * 1024**3
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
MOD_WORDS = re.compile(
    r"[\s._-]*\b(?:vi[ệe]t\s*h[oó][aá]|vietnamese|vietsub|patch|mod)\b[\s._-]*$",
    re.IGNORECASE,
)


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
    # Later sources win: build-time values, then .env, then the environment.
    values = {}
    try:
        import patchvault_build_config  # pyright: ignore[reportMissingImports]

        values.update(patchvault_build_config.VALUES)
    except ImportError:
        pass
    if not getattr(sys, "frozen", False):
        values.update(parse_dotenv(PROJECT_ROOT / ".env"))
    values.update({key: value for key, value in os.environ.items() if key in CONFIG_KEYS})
    return values


def base_dir():
    # Packaged builds keep data next to the program, source runs in .data.
    if not getattr(sys, "frozen", False):
        return PROJECT_ROOT / ".data"
    executable = Path(sys.executable).resolve()
    if sys.platform != "darwin":
        return executable.parent
    # Name.app/Contents/MacOS/Name: use the folder holding the .app. A quarantined app runs
    # from a read-only randomized path, so fall back to the user's Application Support.
    folder = executable.parents[3]
    if "AppTranslocation" in folder.parts or not os.access(folder, os.W_OK):
        return Path.home() / "Library" / "Application Support" / "PatchVault"
    return folder


def app_version():
    return load_config().get(VERSION_KEY) or "dev"
