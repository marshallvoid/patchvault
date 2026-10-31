import hashlib
import http.client
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config as settings
from . import net, system, zips
from .catalog import clean_mapping, fetch_catalog, https_or_empty, parse_catalog
from .config import (
    ASSET_DIGEST,
    BACKUP_DIR,
    CATALOGS_DIR,
    CHUNK,
    DOWNLOAD_DIR,
    MAX_TITLES,
    MAX_UPDATE,
    RETRIES,
    TITLES_FILE,
    TITLES_MAX_AGE,
    TITLES_URL,
    VERSION_KEY,
)
from .names import (
    art_slug,
    clean_game_name,
    display_version,
    format_size,
    game_folder,
    normalized_path,
    parse_titles,
    parse_version,
    safe_name,
    title_name,
)
from .normalizer import back_up, plan_mods
from .system import asset_name, extract_program, swap_program
from .zips import Cancelled, repack_folder, sha256_of


def log_line(text, level):
    return level, f"{datetime.now():%H:%M:%S}  {text}"


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
        self.normalize_job: dict = {
            "phase": "idle",
            "plans": [],
            "done": 0,
            "total": 0,
            "message": "",
            "failed": False,
            "bytes_done": 0,
            "bytes_total": 0,
            "log": [],
            "runs": 0,
        }
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
        named = []
        for item in items:
            catalog_name = item.get("catalog_name", item["name"])
            name = title_name(self.titles, item["title_id"].upper()) or catalog_name
            named.append(dict(item, name=name, catalog_name=catalog_name))
        return named

    def _save_registry(self):
        active = self.catalog["folder"] if self.catalog else None
        self._write_json(
            self.registry_path,
            {
                "active": active,
                "catalogs": self.catalogs,
                "mod_name": self.mod_name,
                "download_dir": self.download_dir,
                "normalize_dest": self.normalize_dest,
            },
        )

    def _download_root(self):
        return Path(self.download_dir).expanduser().resolve() if self.download_dir else self.base / DOWNLOAD_DIR

    def set_download_dir(self, text):
        # Workers read self.root, so it stays put while anything is downloading.
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
        # A reviewed scan shows paths built from the old name, so it has to be redone.
        name = safe_name(name)
        with self.lock:
            if name == self.mod_name or self.normalizing():
                return
            self.mod_name = name
            self._save_registry()
            self._drop_scan("Tên mod đã đổi, hãy bấm Quét lại.")

    def add_catalog(self, name, url, mapping=None):
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
        config = settings.load_config()
        current = parse_version(config.get(VERSION_KEY, ""))
        url = config.get("PATCHVAULT_RELEASES_URL")
        if current is None or not url:
            return "unversioned"
        try:
            with net.http_get(url, {"Accept": "application/vnd.github+json"}) as response:
                release = json.loads(response.read())
            latest = parse_version(release["tag_name"])
            page = https_or_empty(release.get("html_url"))
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            http.client.HTTPException,
        ):
            return "error"
        if latest is None:
            return "error"
        if latest <= current:
            return "current"
        if not page:
            return "error"
        with self.lock:
            self.app_update = {
                "tag": release["tag_name"],
                "url": page,
                "asset": self.pick_asset(release),
            }
        return "available"

    @staticmethod
    def pick_asset(release):
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == asset_name():
                url = asset.get("browser_download_url")
                size = asset.get("size")
                digest = asset.get("digest")
                valid = (
                    isinstance(url, str)
                    and url.startswith("https://github.com/")
                    and isinstance(size, int)
                    and 0 < size <= MAX_UPDATE
                    and isinstance(digest, str)
                    and ASSET_DIGEST.match(digest)
                )
                return {"url": url, "size": size, "sha256": str(digest)[len("sha256:") :]} if valid else None
        return None

    def set_update_job(self, **fields):
        with self.lock:
            self.app_update_job = {**(self.app_update_job or {}), **fields}

    def download_update(self, asset, path):
        digest = hashlib.sha256()
        done = 0
        with net.http_get(asset["url"]) as response, open(path, "wb") as handle:
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
        program = system.program_path()
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
        except (
            OSError,
            ValueError,
            RuntimeError,
            zipfile.BadZipFile,
            subprocess.SubprocessError,
            http.client.HTTPException,
        ) as error:
            self.set_update_job(phase="error", message=str(error) or type(error).__name__)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def local_path(self, record):
        path = (self.root / record["file"]).resolve()
        return path if self.root in path.parents else None

    def game_folders(self, entry):
        # Older saves may use the catalog's name or keep symbols that names now lose.
        names = {entry["name"], entry.get("catalog_name", entry["name"])}
        folders = {game_folder(name, self.mod_name) for name in names}
        folders |= {f"{safe_name(name)} - [{self.mod_name}]" for name in names}
        return [self.root / folder for folder in folders]

    def saved_zips(self, entry):
        zips = []
        for folder in self.game_folders(entry):
            try:
                zips += [path for path in folder.iterdir() if path.suffix.lower() == ".zip" and path.is_file()]
            except OSError:
                pass
        return zips

    def status_of(self, entry):
        # The disk decides if a mod exists; the record only says which release it is.
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
        return {
            "count": 0,
            "finished": 0,
            "total": 0,
            "passed": 0,
            "saved": 0,
            "failed": 0,
            "cancelled": 0,
        }

    def batch_progress(self):
        # Without sizes in the catalog, only finished mods can be counted.
        with self.lock:
            batch = self.batch
            running = sum(job["done"] for job in self.jobs.values() if job["phase"] != "error")
            if batch["total"]:
                fraction = (batch["passed"] + running) / batch["total"]
            else:
                fraction = batch["finished"] / max(1, batch["count"])
            return {
                "active": self.busy(),
                "number": self.batch_number,
                "count": batch["count"],
                "finished": batch["finished"],
                "saved": batch["saved"],
                "failed": batch["failed"],
                "cancelled": batch["cancelled"],
                "fraction": min(1, fraction),
            }

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
                    self.jobs[entry_id] = {
                        "phase": "queued",
                        "done": 0,
                        "total": entry["size"],
                        "speed": 0,
                    }
                    self.cancels[entry_id] = threading.Event()
                    self.queue.put(entry)
                    self._log_game(entry, "đã thêm vào hàng đợi")

    def cancel(self, entry_id):
        with self.lock:
            event = self.cancels.get(entry_id)
        if event:
            event.set()

    def cancel_all(self):
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
        size = entry["size"]  # 0 means the catalog did not say, so size checks are skipped
        have = part.stat().st_size if part.exists() else 0
        if size and have > size:
            part.unlink()
            have = 0
        if size and have == size:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            response = net.http_get(url, headers)
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
                        self._update_job(
                            entry["id"],
                            phase="downloading",
                            done=done,
                            speed=(done - mark_done) / (now - mark),
                        )
                        mark, mark_done = now, done
                self._update_job(entry["id"], done=done)
            if size and done < size:
                raise OSError("kết nối bị ngắt giữa chừng")

    def _download(self, entry, cancel):
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
                        self._log_game(entry, "catalog không có SHA-256, bỏ qua kiểm tra", "warn")
                        return part
                    self._update_job(entry["id"], phase="verifying", speed=0)
                    self._log_game(entry, "đang kiểm tra SHA-256")
                    if sha256_of(part) == entry["sha256"]:
                        return part
                    last_error = "SHA-256 không khớp"
                    self._log_game(entry, f"{last_error}", "error")
                    part.unlink(missing_ok=True)
                    break
                time.sleep(1 + attempt)
            if url != entry["urls"][-1]:
                self._log_game(entry, "chuyển sang nguồn tải khác", "warn")
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
        # The new file is safe by now, so a failure here is not a failed download.
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
                self._log_game(
                    entry,
                    (f"bắt đầu tải ({format_size(entry['size'])})" if entry["size"] else "bắt đầu tải"),
                )
                part = self._download(entry, cancel)
                self._update_job(entry_id, phase="repacking", speed=0)
                self._log_game(entry, "đang đóng gói")
                zips.repack(part, target, entry["title_id"], cancel)
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
        # A failed refresh keeps the old copy, which is empty on a first run offline.
        path = self.catalogs_dir / TITLES_FILE
        names = self._load_json(path, {})
        try:
            if names and time.time() - path.stat().st_mtime < TITLES_MAX_AGE:
                return names
        except OSError:
            pass
        try:
            with net.http_get(TITLES_URL, timeout=60) as response:
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
        self._set_normalize(
            phase="scanning",
            plans=[],
            done=0,
            total=0,
            message="",
            failed=False,
            log=[],
        )
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
            zips.repack(
                plan["source"],
                plan["target"],
                plan["title_id"],
                cancel,
                on_bytes=advance,
            )

    def _finish_plan(self, plan, **fields):
        # The overall bar counts the bytes of a failed mod as passed.
        with self.lock:
            self.normalize_job["bytes_done"] += plan["size"] - plan["written"]
            plan.update(fields, written=plan["size"])

    def _back_up(self, path, root, backup, plans):
        try:
            if path.exists():
                target = back_up(path, root, backup)
                self._log(f"Đã chuyển vào backup: {target.relative_to(backup)}")
        except (OSError, shutil.Error) as error:
            self._log(
                f"Không chuyển được {path.relative_to(root)} vào backup: {error}",
                "error",
            )
            with self.lock:
                for plan in plans:
                    plan["message"] = f"Đã lưu nhưng không chuyển được file gốc vào backup: {error}"

    def _back_up_sources(self, plans, root, backup):
        # A top-level folder moves whole only once every mod in it is saved.
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
        with self.lock:
            job = self.normalize_job
            plans, root, backup = (
                job["plans"],
                job.get("root"),
                job["dest"] / BACKUP_DIR,
            )
            ready = [plan for plan in plans if plan["status"] == "ready"]
            job.update(
                phase="working",
                done=0,
                total=len(ready),
                message="",
                bytes_done=0,
                bytes_total=sum(plan["size"] for plan in ready),
            )
        cancel = self.normalize_cancel
        cancel.clear()
        self._log(f"Bắt đầu chuẩn hóa {len(ready)} mod")
        for plan in plans:
            if plan["status"] in ("conflict", "error"):
                self._log(
                    f"Bỏ qua {clean_game_name(plan['name'])}: {plan['message']}",
                    "error" if plan["status"] == "error" else "warn",
                )
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
