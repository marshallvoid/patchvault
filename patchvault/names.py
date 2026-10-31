import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from .config import (
    DISPLAY_VERSION,
    IMAGE_SUFFIXES,
    INTERNAL_VERSION,
    MISSING_VERSION,
    MOD_WORDS,
    ROMAN_NUMERAL,
    TITLE_ID,
    VERSION_IN_NAME,
    WRAPPER_NAMES,
)


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


def parse_version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text).strip())
    return tuple(int(part) for part in match.groups()) if match else None


def display_version(game_version):
    match = DISPLAY_VERSION.search(game_version)
    return match.group(0) if match else MISSING_VERSION


def clean_mod_name(text):
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}", " ", unicodedata.normalize("NFC", text))
    text = VERSION_IN_NAME.sub(" ", text).replace("_", " ")
    text = re.sub(r"\s+", " ", MOD_WORDS.sub("", text)).strip(" .-")
    return "" if text.lower() in WRAPPER_NAMES or TITLE_ID.match(text) else text


def parse_titles(data):
    names = {}
    for row in data.values() if isinstance(data, dict) else ():
        usable = isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("name"), str)
        if usable and TITLE_ID.match(row["id"]) and row["name"].strip():
            names.setdefault(row["id"].upper(), row["name"].strip())
    return names


def title_name(names, title):
    # Update title IDs end in 800 and otherwise match the base game.
    name = names.get(title) or names.get(title[:-3] + "000") or ""
    return re.sub(r"[™®©]", "", name)


def tidy_capitals(name):

    def tidy(word):
        text = word.group(0)
        if TITLE_ID.match(text):
            return text
        return re.sub(
            r"[^\W\d_]+",
            lambda part: part.group(0) if ROMAN_NUMERAL.fullmatch(part.group(0)) else part.group(0).capitalize(),
            text,
        )

    return re.sub(r"[^\s-]+", tidy, name) if name.isupper() else name


def strip_accents(text):
    # Marks on other scripts stay, since they change the letter there.
    kept = []
    for char in unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D")):
        if not (unicodedata.category(char) == "Mn" and kept and ord(kept[-1]) < 0x250):
            kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def clean_game_name(name):
    text = re.sub(r"[/\\|]+", " ", strip_accents(name))
    text = "".join(char for char in text if char in " -" or char.isspace() or unicodedata.category(char)[0] in "LNM")
    text = re.sub(r"\s+", " ", text).strip(" -")[:120]
    return tidy_capitals(text) or safe_name(name)


def game_folder(name, mod):
    return f"{clean_game_name(name)} - [{mod}]"


def normalized_path(name, version, mod, internal=INTERNAL_VERSION):
    return Path(game_folder(name, mod)) / f"[{display_version(version)}-{internal}] [{mod}].zip"


def format_size(value):
    if value >= 1e9:
        return f"{value / 1e9:.2f} GB"
    if value >= 1e6:
        return f"{value / 1e6:.1f} MB"
    return f"{max(1, round(value / 1e3))} KB"
