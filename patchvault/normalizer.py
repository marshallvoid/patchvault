import os
import shutil
import zipfile
from pathlib import Path

from .config import (
    FIRST_GAME_TITLE,
    INTERNAL_VERSION,
    LEADING_TAG,
    TITLE_ID,
    TRAILING_FOLDER_TAG,
    TRAILING_TAG,
    VERSION_IN_NAME,
    VERSION_PAIR,
)
from .names import (
    clean_mod_name,
    display_version,
    normalized_path,
    safe_name,
    title_name,
)
from .zips import folder_files, plan_repack, zip_titles


def find_mods(root):
    # Title ID folders side by side are one pack, except directly in root.
    found = []
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        titles = [Path(folder) / name for name in dirs if TITLE_ID.match(name)]
        if titles:
            (
                found.extend(("folder", [title]) for title in titles)
                if Path(folder) == root
                else found.append(("folder", titles))
            )
        dirs[:] = [name for name in dirs if not TITLE_ID.match(name) and name != "__MACOSX"]
        found.extend(("zip", [Path(folder) / name]) for name in sorted(files) if name.lower().endswith(".zip"))
    return found


def internal_version(kind, path, root, version):
    # Only a pair matching the chosen game version counts.
    relative = path.relative_to(root)
    names = ([relative.stem] if kind == "zip" else []) + [parent.name for parent in relative.parents if parent.name]
    pairs = (VERSION_PAIR.search(name) for name in names)
    wanted = display_version(version).lstrip("vV")
    return next(
        (pair.group(2) for pair in pairs if pair and pair.group(1).lstrip("vV") == wanted),
        INTERNAL_VERSION,
    )


def guess_details(kind, path, root):
    relative = path.relative_to(root)
    names = ([relative.stem] if kind == "zip" else []) + [parent.name for parent in relative.parents if parent.name]
    name = next(filter(None, map(clean_mod_name, names)), "")
    versions = (VERSION_IN_NAME.search(candidate) for candidate in names)
    version = next((match.group(0) for match in versions if match), "")
    return name, version


def source_mod_name(kind, path, root):
    # A folder that is only a [tag] groups mods rather than naming one.
    relative = path.relative_to(root)
    tags = [TRAILING_TAG.search(relative.stem)] if kind == "zip" else []
    for parent in relative.parents:
        if parent.name:
            tags += [
                LEADING_TAG.match(parent.name),
                TRAILING_FOLDER_TAG.search(parent.name),
            ]
    for tag in filter(None, tags):
        name = safe_name(tag.group(1))
        if name and not VERSION_IN_NAME.search(name) and not name.endswith(f"-{INTERNAL_VERSION}"):
            return name
    return ""


def is_game_title(title):
    return int(title, 16) >= FIRST_GAME_TITLE


def main_title(titles, names, known):
    # Prefer a title known to be a game, then any that looks like a game.
    games = [title for title in titles if is_game_title(title)]
    return (
        next(
            (title for title in games if title_name(names, title) or title in known),
            None,
        )
        or next(iter(games), None)
        or titles[0]
    )


def title_plan(kind, paths, titles, scan, message=None, size=0):
    title = main_title(titles, scan["names"], scan["known"]) if titles else ""
    guess_name, guess_version = guess_details(kind, paths[0], scan["root"])
    entry = scan["known"].get(title)
    official = title_name(scan["names"], title) or (entry and entry["name"])
    name = official or guess_name or title
    version = (entry and entry["game_version"]) or guess_version
    source = paths[0] if len(paths) == 1 else paths[0].parent
    mod = source_mod_name(kind, paths[0], scan["root"]) or scan["mod"]
    internal = internal_version(kind, paths[0], scan["root"], version)
    return {
        "kind": kind,
        "source": source,
        "paths": paths,
        "title_id": title,
        "extra_titles": len(titles) - 1,
        "name": name,
        "version": version,
        "mod": mod,
        "internal": internal,
        "stored": paths[0].stat().st_size if kind == "zip" else size,
        "guessed": not official,
        "target": scan["dest"] / normalized_path(name, version, mod, internal),
        "status": "error" if message else "ready",
        "message": message or "",
        "size": size,
        "written": 0,
    }


def separate_clashes(plans, scan):
    # Different titles with guessed names can land on one path. The same title twice stays a duplicate.
    groups = {}
    for plan in plans:
        groups.setdefault(str(plan["target"]).casefold(), []).append(plan)
    for group in groups.values():
        if len({plan["title_id"] for plan in group}) > 1:
            for plan in group:
                if plan["guessed"]:
                    plan["name"] = f"{plan['name']} {plan['title_id']}"
                    plan["target"] = scan["dest"] / normalized_path(
                        plan["name"], plan["version"], plan["mod"], plan["internal"]
                    )


def mark_conflicts(plans):
    # Normalizing never overwrites, so a taken path keeps its source in place.
    seen = set()
    for plan in plans:
        if plan["status"] != "ready":
            continue
        key = str(plan["target"]).casefold()
        if plan["target"].exists():
            plan["status"], plan["message"] = "conflict", "Đã có file này ở thư mục lưu"
        elif key in seen:
            plan["status"], plan["message"] = (
                "conflict",
                "Trùng với một mod khác trong lần quét này",
            )
        seen.add(key)


def plan_mods(root, dest, known, names, mod):
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
    candidate, number = path, 2
    while candidate.exists():
        name = f"{path.name} ({number})" if path.is_dir() else f"{path.stem} ({number}){path.suffix}"
        candidate, number = path.with_name(name), number + 1
    return candidate


def back_up(path, root, backup):
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
