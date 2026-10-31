import json
import re
import time

from . import net
from .config import GDRIVE_URL, MAX_CATALOG, SAFE_GDRIVE, SAFE_ID, SAFE_SHA
from .names import art_slug


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
    "sha256": [
        "sha256",
        "sha",
        "hash",
        "checksum",
        "sha_256",
        "files.0.sha256",
        "tap_tin.0.sha256",
        "file.sha256",
    ],
    "size": [
        "size",
        "dung_luong",
        "dung_luong_tap_tin",
        "filesize",
        "file_size",
        "bytes",
        "files.0.size",
        "tap_tin.0.dung_luong",
        "file.size",
    ],
    "url": [
        "url",
        "download",
        "download_url",
        "link",
        "tai_ve",
        "duong_dan",
        "files.0.url",
        "tap_tin.0.tai_ve",
        "tap_tin.0.url",
        "file.url",
    ],
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
ROOT_ALIASES = [
    "mods",
    "games",
    "items",
    "data",
    "list",
    "catalog",
    "patches",
    "danh_sach",
]


# Fields the "advanced" panel of the catalog dialog lets the user map by hand.
MAPPABLE_FIELDS = [
    ("__root__", "Mảng game (gốc)"),
    ("id", "Mã (id)"),
    ("name", "Tên game"),
    ("title_id", "Title ID"),
    ("version", "Phiên bản việt hoá"),
    ("game_version", "Phiên bản game"),
    ("url", "Link tải"),
    ("sha256", "SHA-256"),
    ("size", "Dung lượng"),
    ("cover", "Ảnh bìa"),
]


def dig(obj, path):
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
    if not isinstance(mapping, dict):
        return {}
    return {field: key.strip() for field, key in mapping.items() if isinstance(key, str) and key.strip()}


def resolve_urls(primary, mirrors):
    urls = [https_or_empty(primary)]
    for mirror in as_list(mirrors):
        if isinstance(mirror, str) and mirror.startswith("gdrive:"):
            file_id = mirror[len("gdrive:") :]
            if SAFE_GDRIVE.match(file_id):
                urls.append(GDRIVE_URL + file_id)
        else:
            urls.append(https_or_empty(mirror))
    return [url for url in dict.fromkeys(urls) if url]


def normalize(raw, mapping=None):
    # Catalog data is untrusted. An id and one https URL are required, the rest may be missing.
    if not isinstance(raw, dict):
        return None

    def get(field):
        return pick(raw, field, mapping)

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
        "highlights": [
            {"title": as_text(dig(entry, "title")), "text": as_text(dig(entry, "text"))}
            for entry in as_list(get("highlights"))
            if isinstance(entry, dict)
        ],
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
    separator = "&" if "?" in url else "?"
    with net.http_get(f"{url}{separator}t={int(time.time())}") as response:
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
