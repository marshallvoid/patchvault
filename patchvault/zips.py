import hashlib
import os
import time
import zipfile
from pathlib import Path

from .config import CHUNK, JUNK_NAMES, MAX_UNPACKED, TITLE_ID


def plan_repack(infos, title_id):
    # Every title stays: a pack's extra titles, such as a system font, belong to the game.
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
        arcname = "/".join(
            [
                "contents",
                title_id if ours else parts[index].upper(),
                *parts[index + 1 :],
            ]
        )
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
        info._compresslevel = 9  # type: ignore[attr-defined]
    return info


def add_member(output, arcname, date_time, size, reader, written, on_bytes=None):
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
    # Members are streamed, never extracted to disk.
    temp = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
            temp, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as output:
            plan = plan_repack(original.infolist(), title_id)
            written = set()
            for arcname, member in plan.items():
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                with original.open(member) as reader:
                    add_member(
                        output,
                        arcname,
                        member.date_time,
                        member.file_size,
                        reader,
                        written,
                        on_bytes,
                    )
            expected = {name: member.CRC for name, member in plan.items()}
        with zipfile.ZipFile(temp) as result:
            actual = {i.filename: i.CRC for i in result.infolist() if not i.is_dir()}
            if actual != expected or result.testzip() is not None:
                raise RuntimeError("zip đóng gói lại không khớp với zip gốc")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def folder_files(folder, prefix=""):
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
    stamp = time.localtime(path.stat().st_mtime)[:6]
    return min(max(stamp, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))


def repack_folder(folders, target, cancel=None, on_bytes=None):
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


def zip_titles(infos):
    titles = []
    for info in infos:
        parts = info.filename.replace("\\", "/").split("/")
        if parts[0] != "__MACOSX":
            title = next((part.upper() for part in parts[:-1] if TITLE_ID.match(part)), None)
            if title and title not in titles:
                titles.append(title)
    return titles


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Cancelled(Exception):
    pass
