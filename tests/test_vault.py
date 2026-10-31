import hashlib
import io
import json
import random
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

import patchvault as vault  # noqa: E402

TITLE = "0100BA4026E94000"

def entry(**overrides):
    fields = {"id": "game", "sha256": "0" * 64, "size": 1, "url": "https://example.com/a.zip",
              "name": "Game", "title_id": TITLE}
    return {**fields, **overrides}


class ConfigTests(unittest.TestCase):
    def test_reads_dotenv_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('# comment\n\nA=1\nB = "two=2"\nC=\n', encoding="utf-8")
            self.assertEqual(vault.parse_dotenv(path), {"A": "1", "B": "two=2", "C": ""})

    def test_environment_overrides_dotenv(self):
        with mock.patch.object(vault, "parse_dotenv", return_value={"PATCHVAULT_RELEASES_URL": "file"}), \
                mock.patch.dict(vault.os.environ, {"PATCHVAULT_RELEASES_URL": "env", "OTHER": "x"}):
            config = vault.load_config()
        self.assertEqual(config["PATCHVAULT_RELEASES_URL"], "env")
        self.assertNotIn("OTHER", config)


class CatalogManagementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.store = vault.Vault(self.dir)

    def served(self, games):
        raw = json.dumps({"mods": [entry(id=game, name=game.title()) for game in games]}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = raw
        return mock.patch.object(vault, "http_get", return_value=response)

    def add(self, name, url, games=("one",)):
        with self.served(games):
            return self.store.add_catalog(name, url)

    def test_starts_empty(self):
        self.assertIsNone(self.store.catalog)
        self.assertEqual(self.store.items, [])
        self.store.check()
        self.assertIsNone(self.store.check_error)

    def test_adding_switches_to_the_new_catalog(self):
        self.add("First One", "https://a.test/c.json")
        self.assertEqual(self.store.catalog["folder"], "first-one")
        self.assertEqual([item["id"] for item in self.store.items], ["one"])

    def test_rejects_bad_urls_and_duplicates(self):
        with self.assertRaises(ValueError):
            self.store.add_catalog("x", "http://a.test/c.json")
        self.add("x", "https://a.test/c.json")
        with self.assertRaises(ValueError):
            self.store.add_catalog("y", "https://a.test/c.json")

    def test_rejects_files_that_are_not_catalogs(self):
        for body in (b"not json", b"[]", b'{"mods": []}'):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            with mock.patch.object(vault, "http_get", return_value=response), self.assertRaises(ValueError):
                self.store.add_catalog("x", "https://a.test/c.json")
        self.assertEqual(self.store.catalogs, [])

    def test_same_name_gets_its_own_folder(self):
        self.add("Same", "https://a.test/1.json")
        self.assertEqual(self.add("Same", "https://a.test/2.json")["folder"], "same-2")

    def test_catalogs_keep_separate_games_and_state(self):
        self.store.set_mod_name("VI")
        first = self.add("A", "https://a.test/c.json", ("one",))
        item = self.store.items[0]
        target = self.store.root / vault.normalized_path(item["name"], item["game_version"], "VI")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"zip")
        self.store._commit(item, target)
        self.add("B", "https://b.test/c.json", ("one", "two"))
        self.assertEqual(len(self.store.items), 2)
        self.assertEqual(self.store.status_of(self.store.items[0]), "missing")
        self.store.select_catalog(first["folder"])
        self.assertEqual(len(self.store.items), 1)
        self.assertEqual(self.store.status_of(self.store.items[0]), "ok")

    def test_selection_survives_a_restart(self):
        first = self.add("A", "https://a.test/c.json")
        self.add("B", "https://b.test/c.json")
        self.store.select_catalog(first["folder"])
        reopened = vault.Vault(self.dir)
        self.assertEqual(reopened.catalog["folder"], "a")
        self.assertEqual(len(reopened.catalogs), 2)
        self.assertEqual(len(reopened.items), 1)

    def test_removing_keeps_downloads_and_moves_on(self):
        self.add("A", "https://a.test/c.json")
        second = self.add("B", "https://b.test/c.json")
        kept = self.store.root / "kept.zip"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"zip")
        self.store.remove_catalog(second["folder"])
        self.assertEqual(self.store.catalog["folder"], "a")
        self.assertTrue(kept.exists())
        self.store.remove_catalog("a")
        self.assertIsNone(self.store.catalog)

    def test_cannot_add_while_downloading(self):
        self.store.jobs["x"] = {"phase": "downloading"}
        with self.assertRaises(ValueError):
            self.add("x", "https://a.test/c.json")

    def test_check_failure_keeps_the_old_list(self):
        self.add("A", "https://a.test/c.json")
        with mock.patch.object(vault, "http_get", side_effect=OSError("offline")):
            self.store.check()
        self.assertEqual(len(self.store.items), 1)
        self.assertEqual(self.store.check_error, "offline")

    def test_a_late_result_does_not_touch_the_new_catalog(self):
        self.add("A", "https://a.test/c.json")
        self.add("B", "https://b.test/c.json", ("two", "three"))
        response = mock.MagicMock()

        def switch_then_read(*_):
            self.store.select_catalog("a")
            return json.dumps({"mods": [entry(id="late")]}).encode()

        response.__enter__.return_value.read.side_effect = switch_then_read
        with mock.patch.object(vault, "http_get", return_value=response):
            self.store.check()
        self.assertEqual([item["id"] for item in self.store.items], ["one"])


class CatalogTests(unittest.TestCase):
    def test_rejects_unsafe_entries(self):
        self.assertIsNone(vault.normalize(entry(id="../x")))
        self.assertIsNone(vault.normalize(entry(url="http://example.com/a.zip")))
        # A bad or missing hash no longer rejects the entry; verification simply degrades.
        self.assertEqual(vault.normalize(entry(sha256="abc"))["sha256"], "")

    def test_resolves_gdrive_mirrors(self):
        item = vault.normalize(entry(mirrors=["gdrive:abcdefghij1234"]))
        self.assertEqual(item["urls"][1], vault.GDRIVE_URL + "abcdefghij1234")

    def test_target_path_uses_the_vault_format(self):
        item = vault.normalize(entry(name="One: Two/Three", game_version="1.0.6 (kèm DLC)"))
        path = vault.normalized_path(item["name"], item["game_version"], "Bản A")
        self.assertEqual(path.as_posix(), "One Two Three - [Bản A]/[1.0.6-unreviewed] [Bản A].zip")

    def test_missing_game_version(self):
        item = vault.normalize(entry())
        self.assertEqual(vault.normalized_path(item["name"], item["game_version"], "VI").name,
                         "[miss-unreviewed] [VI].zip")


class ArtworkTests(unittest.TestCase):
    def test_file_names_come_from_the_game_name(self):
        cases = {"Castlevania: Belmont's Curse": "castlevania-belmonts-curse",
                 "Demon Slayer -Kimetsu no Yaiba- The Hinokami Chronicles":
                     "demon-slayer-kimetsu-no-yaiba-the-hinokami-chronicles",
                 "Pokémon Đỏ": "pokemon-do"}
        for name, slug in cases.items():
            self.assertEqual(vault.art_slug(name), slug)

    def test_file_name_has_kind_and_a_safe_extension(self):
        item = {"id": "a", "name": "Game One", "cover": "https://x.test/a/c.JPG?v=1", "background": "https://x.test/b"}
        self.assertEqual(vault.art_file_name(item, "cover"), "game-one-cover.jpg")
        self.assertEqual(vault.art_file_name(item, "background"), "game-one-background.img")

    def test_file_name_falls_back_to_the_id(self):
        item = {"id": "a", "name": "???", "cover": "https://x.test/c.png"}
        self.assertEqual(vault.art_file_name(item, "cover"), "a-cover.png")


class RepackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def zip_with(self, names):
        path = self.dir / "source.zip"
        with zipfile.ZipFile(path, "w") as output:
            for name in names:
                output.writestr(name, b"data " + name.encode() * 50)
        return path

    def repacked(self, names):
        target = self.dir / "out.zip"
        vault.repack(self.zip_with(names), target, TITLE)
        return zipfile.ZipFile(target)

    def test_layouts_all_become_contents_title_id(self):
        for prefix in ("atmosphere/contents/", "atmosphere\\contents\\", "Wrapper/atmosphere/contents/", "Wrapper/"):
            with self.subTest(prefix=prefix):
                sep = "\\" if "\\" in prefix else "/"
                result = self.repacked([f"{prefix}{TITLE}{sep}romfs{sep}a.bin"])
                self.assertEqual(result.namelist(), ["contents/", f"contents/{TITLE}/", f"contents/{TITLE}/romfs/",
                                                     f"contents/{TITLE}/romfs/a.bin"])

    def test_files_use_deflate_and_keep_content(self):
        result = self.repacked([f"{TITLE}/romfs/a.bin"])
        info = result.getinfo(f"contents/{TITLE}/romfs/a.bin")
        self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
        self.assertTrue(result.read(info).startswith(b"data "))

    def test_ignores_instructions_and_junk(self):
        result = self.repacked([f"{TITLE}/romfs/a", "HUONG_DAN.txt", "__MACOSX/x", f"{TITLE}/.DS_Store"])
        self.assertEqual([n for n in result.namelist() if not n.endswith("/")], [f"contents/{TITLE}/romfs/a"])

    def test_rejects_unexpected_layouts(self):
        cases = {"traversal": [f"{TITLE}/../../evil"], "other title": ["0100000000000001/romfs/a"],
                 "stray file": ["romfs/a.bin"], "nothing": ["x.txt"],
                 "case clash": [f"a/{TITLE}/romfs/F", f"b/{TITLE}/romfs/f"]}
        for name, members in cases.items():
            with self.subTest(name):
                target = self.dir / "out.zip"
                with self.assertRaises(RuntimeError):
                    vault.repack(self.zip_with(members), target, TITLE)
                self.assertFalse(target.exists())


class FlexibleCatalogTests(unittest.TestCase):
    def test_heuristic_reads_alternative_keys(self):
        raw = {"games": [{"title": "Trò Chơi", "download": "https://x.test/a.zip",
                          "phien_ban": "1.2", "the_loai": ["RPG"]}]}
        item = vault.parse_catalog(raw)[0]
        self.assertEqual(item["name"], "Trò Chơi")
        self.assertEqual(item["urls"], ["https://x.test/a.zip"])
        self.assertEqual(item["version"], "1.2")
        self.assertEqual(item["tags"], ["RPG"])
        self.assertEqual(item["id"], "tro-choi")  # derived from the name when no id is given

    def test_top_level_list_is_a_catalog(self):
        self.assertEqual(len(vault.parse_catalog([{"name": "A", "url": "https://x.test/a.zip"}])), 1)

    def test_nested_file_fields(self):
        raw = {"mods": [{"id": "g", "name": "G",
                         "files": [{"url": "https://x.test/a.zip", "sha256": "a" * 64, "size": 10}]}]}
        item = vault.parse_catalog(raw)[0]
        self.assertEqual((item["urls"], item["sha256"], item["size"]), (["https://x.test/a.zip"], "a" * 64, 10))

    def test_manual_mapping_overrides_everything(self):
        raw = {"payload": [{"slug": "pack1", "tenGame": "Game", "tai": "https://x.test/a.zip"}]}
        mapping = {"__root__": "payload", "id": "slug", "name": "tenGame", "url": "tai"}
        item = vault.parse_catalog(raw, mapping)[0]
        self.assertEqual((item["id"], item["name"], item["urls"]), ("pack1", "Game", ["https://x.test/a.zip"]))

    def test_size_accepts_a_formatted_string(self):
        self.assertEqual(vault.normalize({"id": "g", "url": "https://x.test/a.zip", "size": "1,234"})["size"], 1234)

    def test_optional_fields_default_safely(self):
        item = vault.normalize({"id": "g", "url": "https://x.test/a.zip"})
        self.assertEqual((item["sha256"], item["size"], item["title_id"]), ("", 0, ""))

    def test_clean_mapping_drops_blanks(self):
        self.assertEqual(vault.clean_mapping({"id": " slug ", "name": "", "url": None}), {"id": "slug"})


class VersionFallbackTests(unittest.TestCase):
    def test_update_detection_falls_back_to_version(self):
        with tempfile.TemporaryDirectory() as folder:
            store = vault.Vault(folder)
            store.set_mod_name("VI")
            item = vault.normalize({"id": "g", "name": "G", "url": "https://x.test/a.zip",
                                    "mod_version": "1.0", "title_id": TITLE})
            target = store.root / vault.normalized_path(item["name"], item["game_version"], "VI")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"zip")
            store._commit(item, target)
            self.assertEqual(store.status_of(item), "ok")
            self.assertEqual(store.status_of({**item, "version": "1.1"}), "update")


OTHER_TITLE = "0100AAAA00000000"
FONT_TITLE = "0100000000000811"


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name).resolve()
        self.src = self.base / "src"
        self.dest = self.base / "out"
        self.src.mkdir()
        self.store = vault.Vault(self.base / "data")
        self.store.set_mod_name("VI")
        offline = mock.patch.object(vault, "http_get", side_effect=OSError("offline"))
        offline.start()
        self.addCleanup(offline.stop)

    def zip_file(self, relative, members):
        path = self.src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as output:
            for name, data in members.items():
                output.writestr(name, data)
        return path

    def loose(self, relative, data=b"data"):
        path = self.src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self):
        self.store.scan_mods(str(self.src), str(self.dest))
        return self.store.normalize_job["plans"]

    def names(self, path):
        with zipfile.ZipFile(path) as archive:
            return [info.filename for info in archive.infolist() if not info.is_dir()]

    def test_finds_every_supported_structure(self):
        self.zip_file(f"a/One v1.2 [VI].zip", {f"atmosphere/contents/{TITLE}/romfs/a.bin": b"1"})
        self.loose(f"Two/atmosphere/contents/{OTHER_TITLE}/romfs/a.bin")
        self.loose("Three/contents/0100BBBB00000000/exefs/main")
        self.loose("deep/er/Four/0100CCCC00000000/romfs/a.bin")
        found = {plan["name"]: plan for plan in self.scan()}
        self.assertEqual(set(found), {"One", "Two", "Three", "Four"})
        self.assertEqual(found["One"]["target"], self.dest / "One - [VI]" / "[v1.2-unreviewed] [VI].zip")
        self.assertEqual(found["Two"]["target"], self.dest / "Two - [VI]" / "[miss-unreviewed] [VI].zip")

    def test_moves_a_zip_into_the_vault_layout(self):
        source = self.zip_file(f"pack/Game 1.0.0.zip", {f"x/{TITLE.lower()}/romfs/a.bin": b"1", "readme.txt": b"hi"})
        self.scan()
        self.store.normalize_mods()
        target = self.dest / "Game - [VI]" / "[1.0.0-unreviewed] [VI].zip"
        self.assertEqual(self.names(target), [f"contents/{TITLE}/romfs/a.bin"])
        self.assertFalse(source.exists())
        self.assertFalse(source.parent.exists())
        self.assertTrue(self.src.exists())

    def test_moves_a_loose_folder_and_keeps_its_bytes(self):
        self.loose(f"Game/atmosphere/contents/{TITLE}/romfs/a.bin", b"payload")
        self.loose("Game/notes.txt")
        self.scan()
        self.store.normalize_mods()
        target = self.dest / "Game - [VI]" / "[miss-unreviewed] [VI].zip"
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.read(f"contents/{TITLE}/romfs/a.bin"), b"payload")
        self.assertFalse((self.src / "Game").exists())

    def test_catalog_names_the_title(self):
        known = {TITLE: vault.normalize(entry(name="Catalog Name", game_version="2.0.0"))}
        self.zip_file("bad name 9.9.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        plan = vault.plan_mods(self.src, self.dest, known, {}, "VI")[0]
        self.assertEqual(plan["target"], self.dest / "Catalog Name - [VI]" / "[2.0.0-unreviewed] [VI].zip")

    def test_title_database_names_the_game_and_the_mod_comes_from_the_user(self):
        known = {TITLE: vault.normalize(entry(name="Catalog Name"))}
        self.zip_file("bad name.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        plan = vault.plan_mods(self.src, self.dest, known, {TITLE: "Real: Game™"}, "Bản dịch A")[0]
        self.assertEqual(plan["target"], self.dest / "Real Game - [Bản dịch A]" / "[miss-unreviewed] [Bản dịch A].zip")

    def test_update_title_uses_the_base_game_name(self):
        self.assertEqual(vault.title_name({"0100BA4026E94000": "Game"}, "0100BA4026E94800"), "Game")

    def test_requires_a_mod_name(self):
        self.store.set_mod_name(" / ")
        self.store.scan_mods(str(self.src), str(self.dest))
        self.assertTrue(self.store.normalize_job["failed"])

    def test_reports_progress_and_a_log(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1" * 1000})
        self.loose(f"Two/{OTHER_TITLE}/romfs/b.bin", b"2" * 500)
        self.zip_file("nothing.zip", {"romfs/a.bin": b"1"})
        self.scan()
        self.store.normalize_mods()
        job = self.store.normalize_snapshot()
        self.assertEqual((job["bytes_done"], job["bytes_total"]), (1500, 1500))
        self.assertEqual({plan["written"] for plan in job["plans"] if plan["status"] == "done"}, {1000, 500})
        text = "\n".join(line for _, line in job["log"])
        self.assertIn("Đã lưu", text)
        self.assertIn("Bỏ qua", text)
        self.assertIn("Đã xoá", text)

    def test_failed_mod_counts_as_passed_on_the_overall_bar(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1" * 1000})
        self.scan()
        with mock.patch.object(vault, "repack", side_effect=OSError("disk full")):
            self.store.normalize_mods()
        self.assertEqual(self.store.normalize_job["bytes_done"], 1000)

    def test_saves_both_destination_folders(self):
        self.store.set_download_dir(str(self.base / "mine"))
        self.store.set_normalize_dest(str(self.dest))
        again = vault.Vault(self.base / "data")
        self.assertEqual((again.download_dir, again.normalize_dest), (str(self.base / "mine"), str(self.dest)))
        self.assertEqual(again.root, self.base / "mine" / "_none")

    def test_blank_download_folder_means_the_default(self):
        self.store.set_download_dir(str(self.base / "mine"))
        self.store.set_download_dir("  ")
        self.assertEqual(self.store.root, self.base / "data" / "downloaded" / "_none")

    def place(self, item, name="[1.0-unreviewed] [VI].zip"):
        path = self.store.root / f"{item['name']} - [VI]" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"zip")
        return path

    def destination_item(self):
        self.store.set_download_dir(str(self.base / "mine"))
        return vault.normalize(entry(name="Game", sha256="a" * 64, game_version="1.0"))

    def test_destination_decides_the_status(self):
        item = self.destination_item()
        self.assertEqual(self.store.status_of(item), "missing")
        target = self.place(item)
        self.store._commit(item, target)
        self.assertEqual(self.store.status_of(item), "ok")
        self.assertEqual(self.store.status_of(dict(item, sha256="b" * 64)), "update")
        self.store.set_download_dir(str(self.base / "other"))
        self.assertEqual(self.store.status_of(item), "missing")

    def test_destination_finds_a_moved_copy_after_a_game_version_change(self):
        item = self.destination_item()
        self.store._commit(item, self.place(item))
        self.assertEqual(self.store.status_of(dict(item, sha256="b" * 64, game_version="2.0")), "update")

    def test_hand_copied_file_is_judged_by_its_name(self):
        item = self.destination_item()
        self.place(item)
        self.assertEqual(self.store.status_of(item), "ok")
        self.assertEqual(self.store.status_of(dict(item, game_version="2.0")), "update")

    def test_default_folder_works_like_a_destination(self):
        item = vault.normalize(entry(name="Game", sha256="a" * 64, game_version="1.0"))
        self.assertEqual(self.store.status_of(item), "missing")
        target = self.place(item)
        self.store._commit(item, target)
        self.assertEqual(self.store.status_of(item), "ok")
        self.assertEqual(self.store.status_of(dict(item, sha256="b" * 64)), "update")
        target.unlink()
        self.assertEqual(self.store.status_of(item), "missing")

    def test_state_does_not_store_a_location(self):
        item = self.destination_item()
        self.store._commit(item, self.place(item))
        self.assertNotIn("root", self.store.state[item["id"]])

    def test_status_ignores_a_root_left_by_an_older_version(self):
        item = self.destination_item()
        self.store._commit(item, self.place(item))
        self.store.state[item["id"]]["root"] = str(self.base / "gone")
        self.assertEqual(self.store.status_of(item), "ok")

    def test_new_destination_leaves_the_old_copy_alone(self):
        item = self.destination_item()
        first = self.place(item)
        self.store._commit(item, first)
        self.store.set_download_dir(str(self.base / "other"))
        self.store._commit(item, self.place(item))
        self.assertTrue(first.exists())

    def test_update_inside_a_destination_replaces_the_old_copy(self):
        item = self.destination_item()
        first = self.place(item)
        self.store._commit(item, first)
        self.store._commit(item, self.place(item, "[2.0-unreviewed] [VI].zip"))
        self.assertFalse(first.exists())

    def test_download_folder_stays_put_while_downloading(self):
        self.store.jobs["game"] = {"phase": "downloading"}
        before = self.store.root
        self.store.set_download_dir(str(self.base / "mine"))
        self.assertEqual(self.store.root, before)

    def test_changing_the_destination_drops_the_reviewed_scan(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.scan()
        self.store.set_normalize_dest(str(self.base / "elsewhere"))
        self.assertEqual(self.store.normalize_job["plans"], [])

    def run_worker(self, item):
        self.store.items = [item]
        self.store.enqueue([item["id"]])
        real_get = self.store.queue.get
        calls = iter([real_get])
        def next_entry():
            step = next(calls, None)
            if step is None:
                raise SystemExit
            return step()
        with mock.patch.object(self.store.queue, "get", side_effect=next_entry), self.assertRaises(SystemExit):
            self.store.worker()
        return "\n".join(line for _, line in self.store.snapshot()["log"])

    def test_download_log_follows_a_download(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        item = vault.normalize(entry(name="Game", sha256=""))
        with mock.patch.object(self.store, "_download", return_value=self.src / "Game.zip"):
            text = self.run_worker(item)
        for expected in ("hàng đợi", "bắt đầu tải", "đang đóng gói", "đã lưu"):
            self.assertIn(expected, text)

    def test_download_log_keeps_retries_and_the_failure(self):
        item = vault.normalize(entry(name="Game", sha256="a" * 64))
        with mock.patch.object(vault.time, "sleep"):
            text = self.run_worker(item)
        self.assertEqual(text.count("lỗi tải"), vault.RETRIES)
        self.assertIn("Game: lỗi: offline", text)

    def test_logs_use_the_clean_game_name(self):
        self.store.items = [vault.normalize(entry(name="Game: It's"))]
        self.store.enqueue(["game"])
        self.assertIn("Game Its: đã thêm", self.store.snapshot()["log"][-1][1])
        self.zip_file("Game's Pack.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.scan()
        self.store.normalize_mods()
        self.assertTrue(any("Đang đóng gói Games Pack" in line for _, line in self.store.normalize_snapshot()["log"]))

    def test_logs_mark_what_matters(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.zip_file("nothing.zip", {"romfs/a.bin": b"1"})
        self.scan()
        self.store.normalize_mods()
        levels = {level for level, _ in self.store.normalize_snapshot()["log"]}
        self.assertLessEqual({"info", "ok", "error"}, levels)

    def test_download_log_marks_saved_files(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        with mock.patch.object(self.store, "_download", return_value=self.src / "Game.zip"):
            self.run_worker(vault.normalize(entry(name="Game", sha256="")))
        self.assertEqual(self.store.snapshot()["log"][-1][0], "ok")

    def test_stop_all_cancels_every_download(self):
        items = [vault.normalize(entry(id=name, name=name)) for name in ("a", "b")]
        self.store.items = items
        self.store.enqueue(["a", "b"])
        self.store.cancel_all()
        self.assertTrue(all(event.is_set() for event in self.store.cancels.values()))

    def test_downloads_use_the_title_database_name(self):
        self.store.titles = {TITLE: "Real Game™"}
        item = self.store._named([vault.normalize(entry(name="Catalog Name"))])[0]
        self.assertEqual((item["name"], item["catalog_name"]), ("Real Game", "Catalog Name"))

    def test_unknown_title_keeps_the_catalog_name(self):
        item = self.store._named([vault.normalize(entry(name="Catalog Name"))])[0]
        self.assertEqual(item["name"], "Catalog Name")

    def test_refresh_renames_loaded_games(self):
        self.store.items = [vault.normalize(entry(name="Catalog Name"))]
        with mock.patch.object(self.store, "title_names", return_value={TITLE: "Real"}):
            self.store.refresh_titles()
        self.assertEqual(self.store.items[0]["name"], "Real")

    def test_destination_still_finds_a_save_under_the_catalog_name(self):
        self.store.set_download_dir(str(self.base / "mine"))
        item = vault.normalize(entry(name="Catalog Name", sha256="a" * 64))
        old = self.store.root / "Catalog Name - [VI]" / "[miss-unreviewed] [VI].zip"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"zip")
        self.store._commit(item, old)
        self.store.titles = {TITLE: "Real Name"}
        named = self.store._named([item])[0]
        self.assertEqual(self.store.status_of(named), "ok")
        new = self.store.root / "Real Name - [VI]" / "[miss-unreviewed] [VI].zip"
        new.parent.mkdir(parents=True)
        new.write_bytes(b"zip")
        self.store._commit(named, new)
        self.assertFalse(old.parent.exists())

    def test_game_names_lose_special_characters(self):
        for raw, expected in (("One Piece: Grand Gourmet", "One Piece Grand Gourmet"),
                              ("Castlevania: Belmont's Curse", "Castlevania Belmonts Curse"),
                              ("Fate/Stay Night", "Fate Stay Night"), ("Ori & the Blind Forest", "Ori the Blind Forest"),
                              ("Mario Kart™ 8 Deluxe!", "Mario Kart 8 Deluxe"), ("Spider-Man", "Spider-Man"),
                              ("Trò Chơi Việt", "Trò Chơi Việt"), ("ドラゴンクエスト", "ドラゴンクエスト"), ("!!!", "!!!")):
            self.assertEqual(vault.clean_game_name(raw), expected)

    def test_all_capitals_names_become_title_case(self):
        for raw, expected in (("OCTOPATH TRAVELER 0", "Octopath Traveler 0"), ("SPIDER-MAN", "Spider-Man"),
                              ("FINAL FANTASY VII", "Final Fantasy VII"),
                              ("ĐẠI CHIẾN", "Đại Chiến"), ("SUPER MARIO 3D WORLD", "Super Mario 3D World"),
                              ("Mario KART 8", "Mario KART 8"), ("ドラゴン", "ドラゴン"),
                              (TITLE, TITLE), (f"PACK {TITLE}", f"Pack {TITLE}")):
            self.assertEqual(vault.clean_game_name(raw), expected)

    def test_both_tabs_save_under_the_clean_name(self):
        path = vault.normalized_path("Castlevania: Belmont's Curse", "1.1.0", "VI")
        self.assertEqual(path.as_posix(), "Castlevania Belmonts Curse - [VI]/[1.1.0-unreviewed] [VI].zip")

    def test_finds_and_replaces_a_save_from_before_names_were_cleaned(self):
        self.store.set_download_dir(str(self.base / "mine"))
        item = vault.normalize(entry(name="Castlevania: Belmont's Curse", sha256="a" * 64))
        old = self.store.root / "Castlevania Belmont's Curse - [VI]" / "[miss-unreviewed] [VI].zip"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"zip")
        self.store._commit(item, old)
        self.assertEqual(self.store.status_of(item), "ok")
        new = self.store.root / vault.normalized_path(item["name"], item["game_version"], "VI")
        new.parent.mkdir(parents=True)
        new.write_bytes(b"zip")
        self.store._commit(item, new)
        self.assertFalse(old.parent.exists())

    def test_mod_name_is_saved(self):
        self.store.set_mod_name("Bản A")
        self.assertEqual(vault.Vault(self.base / "data").mod_name, "Bản A")

    def test_changing_the_mod_name_drops_the_reviewed_scan(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.scan()
        self.store.set_mod_name("Khác")
        self.assertEqual(self.store.normalize_job["plans"], [])
        self.assertEqual(self.scan()[0]["target"], self.dest / "Game - [Khác]" / "[miss-unreviewed] [Khác].zip")

    def test_downloads_use_the_same_mod_name(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        item = vault.normalize(entry(name="Game", sha256="", game_version="1.0"))
        self.store.items = [item]
        self.store.enqueue([item["id"]])
        with mock.patch.object(self.store, "_download", return_value=self.src / "Game.zip"), \
                mock.patch.object(self.store, "_commit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.store.worker()
        self.assertTrue((self.store.root / "Game - [VI]" / "[1.0-unreviewed] [VI].zip").exists())

    def test_title_database_is_cached(self):
        raw = json.dumps({"1": {"id": TITLE.lower(), "name": "Game"}, "2": {"id": None, "name": "x"}}).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = raw
        with mock.patch.object(vault, "http_get", return_value=response) as get:
            self.assertEqual(self.store.title_names(), {TITLE: "Game"})
            self.assertEqual(self.store.title_names(), {TITLE: "Game"})
        self.assertEqual(get.call_count, 1)

    def test_old_title_database_survives_a_failed_refresh(self):
        path = self.store.catalogs_dir / vault.TITLES_FILE
        path.write_text(json.dumps({TITLE: "Game"}), encoding="utf-8")
        vault.os.utime(path, (1, 1))
        self.assertEqual(self.store.title_names(), {TITLE: "Game"})

    def test_uses_deflate_level_nine(self):
        words = random.Random(1).choices(["alpha", "beta", "gamma", "delta", "omega", "sigma"], k=60000)
        data = " ".join(words).encode()
        self.loose(f"Game/{TITLE}/romfs/a.bin", data)
        self.scan()
        self.store.normalize_mods()
        with zipfile.ZipFile(self.dest / "Game - [VI]" / "[miss-unreviewed] [VI].zip") as archive:
            info = archive.getinfo(f"contents/{TITLE}/romfs/a.bin")
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
        self.assertEqual(info.compress_size, len(packer.compress(data) + packer.flush()))

    def test_never_overwrites_and_keeps_the_source(self):
        target = self.dest / "Game - [VI]" / "[miss-unreviewed] [VI].zip"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        source = self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.assertEqual(self.scan()[0]["status"], "conflict")
        self.store.normalize_mods()
        self.assertEqual(target.read_bytes(), b"old")
        self.assertTrue(source.exists())

    def test_same_title_twice_keeps_the_second_source(self):
        first = self.zip_file("a/Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        second = self.zip_file("b/Game.zip", {f"{TITLE}/romfs/a.bin": b"2"})
        self.assertEqual([plan["status"] for plan in self.scan()], ["ready", "conflict"])
        self.store.normalize_mods()
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_pack_with_a_font_title_stays_one_zip_named_after_the_game(self):
        source = self.zip_file("Pack.zip", {f"atmosphere/contents/{FONT_TITLE}/romfs/f.ttf": b"1",
                                            f"atmosphere/contents/{TITLE}/romfs/a.bin": b"2"})
        with mock.patch.object(self.store, "title_names", return_value={TITLE: "Real Game"}):
            plans = self.scan()
        self.assertEqual((len(plans), plans[0]["title_id"], plans[0]["extra_titles"]), (1, TITLE, 1))
        self.store.normalize_mods()
        self.assertEqual(sorted(self.names(self.dest / "Real Game - [VI]" / "[miss-unreviewed] [VI].zip")),
                         sorted([f"contents/{TITLE}/romfs/a.bin", f"contents/{FONT_TITLE}/romfs/f.ttf"]))
        self.assertFalse(source.exists())

    def test_loose_pack_stays_one_zip_and_leaves_nothing_behind(self):
        self.loose(f"Game/atmosphere/contents/{FONT_TITLE}/romfs/f.ttf")
        self.loose(f"Game/atmosphere/contents/{TITLE}/romfs/a.bin")
        plans = self.scan()
        self.assertEqual((len(plans), plans[0]["title_id"]), (1, TITLE))
        self.store.normalize_mods()
        self.assertEqual(len(self.names(self.dest / "Game - [VI]" / "[miss-unreviewed] [VI].zip")), 2)
        self.assertFalse((self.src / "Game").exists())

    def test_titles_directly_in_the_source_are_separate_mods(self):
        self.loose(f"{TITLE}/romfs/a.bin")
        self.loose(f"{OTHER_TITLE}/romfs/b.bin")
        self.assertEqual(len(self.scan()), 2)

    def test_two_game_titles_with_one_known_use_the_known_one(self):
        self.assertEqual(vault.main_title([OTHER_TITLE, TITLE], {TITLE: "Game"}, {}), TITLE)
        self.assertEqual(vault.main_title([FONT_TITLE, OTHER_TITLE], {}, {}), OTHER_TITLE)
        self.assertEqual(vault.main_title([FONT_TITLE], {}, {}), FONT_TITLE)

    def test_ignores_everything_outside_the_title_folder(self):
        self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1", "switch/tool.nro": b"2", "manifest.json": b"{}"})
        self.scan()
        self.store.normalize_mods()
        self.assertEqual(self.names(self.dest / "Game - [VI]" / "[miss-unreviewed] [VI].zip"),
                         [f"contents/{TITLE}/romfs/a.bin"])

    def test_deletes_the_outermost_folder(self):
        self.zip_file("outer/inner/Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.loose("outer/other/notes.txt")
        self.loose("elsewhere/notes.txt")
        self.scan()
        self.store.normalize_mods()
        self.assertFalse((self.src / "outer").exists())
        self.assertTrue((self.src / "elsewhere" / "notes.txt").exists())

    def test_keeps_the_outermost_folder_while_a_mod_in_it_is_unfinished(self):
        first = self.zip_file("outer/Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        broken = self.zip_file("outer/nothing.zip", {"romfs/a.bin": b"1"})
        self.scan()
        self.store.normalize_mods()
        self.assertFalse(first.exists())
        self.assertTrue(broken.exists())

    def test_reports_zips_that_cannot_be_used(self):
        self.zip_file("nothing.zip", {"romfs/a.bin": b"1"})
        (self.src / "broken.zip").write_bytes(b"not a zip")
        self.assertEqual([plan["status"] for plan in self.scan()], ["error", "error"])

    def test_keeps_the_source_when_writing_fails(self):
        source = self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.scan()
        with mock.patch.object(vault, "repack", side_effect=OSError("disk full")):
            self.store.normalize_mods()
        self.assertEqual(self.store.normalize_job["plans"][0]["status"], "error")
        self.assertTrue(source.exists())

    def test_rejects_nested_or_missing_folders(self):
        for source, dest in ((self.src, self.src), (self.src, self.src / "out"),
                             (self.base / "nope", self.dest), ("", self.dest)):
            self.store.scan_mods(str(source), str(dest))
            self.assertTrue(self.store.normalize_job["failed"], (source, dest))

    def test_source_may_sit_inside_the_destination(self):
        self.zip_file("outer/Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.store.scan_mods(str(self.src), str(self.base))
        self.store.normalize_mods()
        self.assertTrue((self.base / "Game - [VI]" / "[miss-unreviewed] [VI].zip").exists())
        self.assertFalse((self.src / "outer").exists())
        self.assertTrue(self.src.exists())

    def test_cancelling_keeps_unfinished_sources(self):
        source = self.zip_file("Game.zip", {f"{TITLE}/romfs/a.bin": b"1"})
        self.scan()
        with mock.patch.object(vault, "repack", side_effect=vault.Cancelled):
            self.store.normalize_mods()
        self.assertEqual(self.store.normalize_job["plans"][0]["status"], "ready")
        self.assertTrue(source.exists())

    def test_cleans_names(self):
        for raw, expected in (("[VI] - Game", "Game"), ("[1.0.0-unreviewed] [VI]", ""), ("Game_Name_v1.2", "Game Name"),
                              ("Game Việt Hóa", "Game"), ("contents", ""), (TITLE, ""), ("Final Fantasy VI", "Final Fantasy VI")):
            self.assertEqual(vault.clean_mod_name(raw), expected)


class ReplaceDownloadTests(unittest.TestCase):
    def test_saving_survives_an_old_folder_that_is_already_gone(self):
        with tempfile.TemporaryDirectory() as folder:
            store = vault.Vault(folder)
            item = vault.normalize(entry(name="Game", sha256="a" * 64))
            store.state[item["id"]] = {"file": "Game - []/old.zip", "sha256": "b" * 64, "version": ""}
            target = store.root / "elsewhere" / "new.zip"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"zip")
            store._commit(item, target)
            self.assertEqual(store.state[item["id"]]["sha256"], "a" * 64)


class StatusTests(unittest.TestCase):
    def test_update_detection_uses_original_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            store = vault.Vault(folder)
            store.set_mod_name("VI")
            item = vault.normalize(entry())
            self.assertEqual(store.status_of(item), "missing")
            target = store.root / vault.normalized_path(item["name"], item["game_version"], "VI")
            target.parent.mkdir(parents=True)
            target.write_bytes(b"repacked")
            store._commit(item, target)
            self.assertEqual(store.status_of(item), "ok")
            self.assertEqual(store.status_of({**item, "sha256": "1" * 64}), "update")
            target.unlink()
            self.assertEqual(store.status_of(item), "missing")


class AppUpdateTests(unittest.TestCase):
    URL = "https://example.com/latest"

    def check(self, current, body):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        config = {vault.VERSION_KEY: current, "PATCHVAULT_RELEASES_URL": self.URL}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(vault, "load_config", return_value=config), \
                mock.patch.object(vault, "http_get", return_value=response) as get:
            store = vault.Vault(folder)
            store.check_app_update()
        return store.app_update, get

    def release(self, tag):
        return f'{{"tag_name": "{tag}", "html_url": "https://github.com/o/r/releases/tag/{tag}"}}'.encode()

    def test_picks_the_asset_for_this_platform(self):
        asset = {"name": vault.asset_name(), "size": 5, "digest": "sha256:" + "a" * 64,
                 "browser_download_url": "https://github.com/o/r/releases/download/v1/a.zip"}
        release = {"assets": [{"name": "other.zip"}, asset]}
        self.assertEqual(vault.Vault.pick_asset(release),
                         {"url": asset["browser_download_url"], "size": 5, "sha256": "a" * 64})

    def test_rejects_assets_that_cannot_be_verified(self):
        good = {"name": vault.asset_name(), "size": 5, "digest": "sha256:" + "a" * 64,
                "browser_download_url": "https://github.com/o/r/a.zip"}
        for change in ({"digest": None}, {"digest": "md5:abc"}, {"size": 0}, {"size": vault.MAX_UPDATE + 1},
                       {"browser_download_url": "https://evil.test/a.zip"}):
            self.assertIsNone(vault.Vault.pick_asset({"assets": [{**good, **change}]}))
        self.assertIsNone(vault.Vault.pick_asset({"assets": []}))

    def test_app_version(self):
        with mock.patch.object(vault, "load_config", return_value={vault.VERSION_KEY: "v1.2.3"}):
            self.assertEqual(vault.app_version(), "v1.2.3")
        with mock.patch.object(vault, "load_config", return_value={vault.VERSION_KEY: ""}):
            self.assertEqual(vault.app_version(), "dev")

    def test_reports_the_outcome_of_a_check(self):
        outcomes = {("v1.0.0", self.release("v1.0.1")): "available", ("v1.0.1", self.release("v1.0.1")): "current",
                    ("v1.0.0", b"not json"): "error", ("", self.release("v1.0.1")): "unversioned"}
        for (current, body), outcome in outcomes.items():
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            config = {vault.VERSION_KEY: current, "PATCHVAULT_RELEASES_URL": self.URL}
            with tempfile.TemporaryDirectory() as folder, \
                    mock.patch.object(vault, "load_config", return_value=config), \
                    mock.patch.object(vault, "http_get", return_value=response):
                self.assertEqual(vault.Vault(folder).check_app_update(), outcome)

    def test_parses_versions(self):
        self.assertEqual(vault.parse_version("v1.2.3"), (1, 2, 3))
        for text in ("", "dev", "v1.2", "1.2.3-beta"):
            self.assertIsNone(vault.parse_version(text))

    def test_reports_a_newer_release(self):
        update, _ = self.check("v1.9.0", self.release("v1.10.0"))
        self.assertEqual(update["tag"], "v1.10.0")

    def test_ignores_same_or_older_releases(self):
        for tag in ("v1.2.3", "v1.2.2"):
            self.assertIsNone(self.check("v1.2.3", self.release(tag))[0])

    def test_builds_without_a_version_never_ask(self):
        update, get = self.check("", self.release("v9.0.0"))
        self.assertIsNone(update)
        get.assert_not_called()

    def test_ignores_bad_responses(self):
        for body in (b"not json", b"{}", b"[]", b'{"tag_name": "v9.0.0", "html_url": "http://x.test"}'):
            self.assertIsNone(self.check("v1.0.0", body)[0])


class SelfUpdateTests(unittest.TestCase):
    NAME = "PatchVault"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.program = self.dir / self.NAME
        self.program.write_bytes(b"old program")
        self.data = self.dir / "downloaded" / "game.zip"
        self.data.parent.mkdir()
        self.data.write_bytes(b"my data")

    def release_zip(self, members=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as output:
            for name, content in (members or {self.NAME: b"new program", "downloaded/": b"", "covers/": b""}).items():
                output.writestr(name, content)
        return buffer.getvalue()

    def install(self, body, asset=None):
        digest = hashlib.sha256(body).hexdigest()
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(body)
        asset = {"url": "https://github.com/a", "size": len(body), "sha256": digest, **(asset or {})}
        store = vault.Vault(self.dir)
        with mock.patch.object(vault, "http_get", return_value=response), \
                mock.patch.object(vault, "program_path", return_value=self.program), \
                mock.patch.object(vault.sys, "platform", "linux"):
            store.install_app_update({"asset": asset})
        return store.app_update_job

    def test_replaces_the_program_and_keeps_data_and_a_backup(self):
        job = self.install(self.release_zip())
        self.assertEqual(job["phase"], "done")
        self.assertEqual(self.program.read_bytes(), b"new program")
        self.assertEqual(vault.backup_path(self.program).read_bytes(), b"old program")
        self.assertEqual(self.data.read_bytes(), b"my data")

    def test_new_program_is_executable(self):
        self.install(self.release_zip())
        if sys.platform != "win32":
            self.assertTrue(self.program.stat().st_mode & 0o100)

    def test_wrong_digest_changes_nothing(self):
        job = self.install(self.release_zip(), {"sha256": "0" * 64})
        self.assertEqual(job["phase"], "error")
        self.assertEqual(self.program.read_bytes(), b"old program")
        self.assertFalse(vault.backup_path(self.program).exists())

    def test_oversized_download_is_stopped(self):
        job = self.install(self.release_zip(), {"size": 3})
        self.assertEqual(job["phase"], "error")
        self.assertEqual(self.program.read_bytes(), b"old program")

    def test_zip_without_the_program_changes_nothing(self):
        job = self.install(self.release_zip({"covers/": b"", "other": b"x"}))
        self.assertEqual(job["phase"], "error")
        self.assertEqual(self.program.read_bytes(), b"old program")

    def test_failed_swap_restores_the_old_program(self):
        new = self.dir / "new"
        new.write_bytes(b"new")
        with mock.patch.object(vault.shutil, "move", side_effect=OSError("disk full")), self.assertRaises(OSError):
            vault.swap_program(new, self.program)
        self.assertEqual(self.program.read_bytes(), b"old program")

    def test_next_start_removes_the_backup(self):
        backup = vault.backup_path(self.program)
        backup.write_bytes(b"old")
        with mock.patch.object(vault.sys, "frozen", True, create=True), \
                mock.patch.object(vault, "program_path", return_value=self.program):
            vault.remove_old_program()
        self.assertFalse(backup.exists())

    def test_work_files_are_removed(self):
        self.install(self.release_zip())
        self.assertFalse((self.dir / ".parts" / "app-update").exists())

    def test_only_frozen_builds_with_an_asset_self_update(self):
        self.assertFalse(vault.can_self_update({"asset": {"url": "x"}}))
        with mock.patch.object(vault.sys, "frozen", True, create=True), \
                mock.patch.object(vault, "program_path", return_value=self.program):
            self.assertTrue(vault.can_self_update({"asset": {"url": "x"}}))
            self.assertFalse(vault.can_self_update({"asset": None}))


class PlatformTests(unittest.TestCase):
    def frozen_base(self, platform, executable):
        with mock.patch.object(vault.sys, "frozen", True, create=True), \
                mock.patch.object(vault.sys, "platform", platform), \
                mock.patch.object(vault.sys, "executable", executable), \
                mock.patch.object(vault.os, "access", return_value=True):
            return vault.base_dir()

    def test_source_runs_use_the_data_folder(self):
        self.assertEqual(vault.base_dir(), Path(vault.__file__).resolve().parent / ".data")

    def test_data_sits_next_to_the_program(self):
        self.assertEqual(self.frozen_base("linux", "/opt/vault/App"), Path("/opt/vault"))

    def test_macos_uses_the_folder_holding_the_app(self):
        self.assertEqual(self.frozen_base("darwin", "/Users/a/Games/App.app/Contents/MacOS/App"),
                         Path("/Users/a/Games"))

    def test_macos_translocated_app_uses_application_support(self):
        result = self.frozen_base("darwin", "/private/var/AppTranslocation/X/d/App.app/Contents/MacOS/App")
        self.assertEqual(result.parts[-3:], ("Library", "Application Support", "PatchVault"))


if __name__ == "__main__":
    unittest.main()
