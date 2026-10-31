import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
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
        first = self.add("A", "https://a.test/c.json", ("one",))
        item = self.store.items[0]
        target = self.store.root / vault.target_path(item)
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
        self.assertEqual(vault.target_path(item).as_posix(), "[VI] - One Two Three/[1.0.6-unreviewed] [VI].zip")

    def test_missing_game_version(self):
        item = vault.normalize(entry())
        self.assertEqual(vault.target_path(item).name, "[miss-unreviewed] [VI].zip")


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
            item = vault.normalize({"id": "g", "name": "G", "url": "https://x.test/a.zip",
                                    "mod_version": "1.0", "title_id": TITLE})
            target = store.root / vault.target_path(item)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"zip")
            store._commit(item, target)
            self.assertEqual(store.status_of(item), "ok")
            self.assertEqual(store.status_of({**item, "version": "1.1"}), "update")


class StatusTests(unittest.TestCase):
    def test_update_detection_uses_original_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            store = vault.Vault(folder)
            item = vault.normalize(entry())
            self.assertEqual(store.status_of(item), "missing")
            target = store.root / vault.target_path(item)
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
