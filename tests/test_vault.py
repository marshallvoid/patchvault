import base64
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
# RFC 6979 appendix A.2.5: P-256, SHA-256, message "sample".
RFC_KEY = (0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,
           0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299)
RFC_R = 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716
RFC_S = 0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8


def der_integer(value):
    data = value.to_bytes(33, "big").lstrip(b"\0") or b"\0"
    if data[0] & 0x80:
        data = b"\0" + data
    return bytes([2, len(data)]) + data


def der_signature(r, s):
    body = der_integer(r) + der_integer(s)
    return bytes([0x30, len(body)]) + body


def entry(**overrides):
    fields = {"id": "game", "sha256": "0" * 64, "size": 1, "url": "https://example.com/a.zip",
              "name": "Game", "title_id": TITLE}
    return {**fields, **overrides}


class SignatureTests(unittest.TestCase):
    def test_rfc6979_vector(self):
        signature = der_signature(RFC_R, RFC_S)
        self.assertTrue(vault.verify_ecdsa(b"sample", signature, RFC_KEY))

    def test_rejects_changed_message_or_signature(self):
        self.assertFalse(vault.verify_ecdsa(b"Sample", der_signature(RFC_R, RFC_S), RFC_KEY))
        self.assertFalse(vault.verify_ecdsa(b"sample", der_signature(RFC_R, RFC_S + 1), RFC_KEY))

    def test_rejects_malformed_signatures(self):
        good = der_signature(RFC_R, RFC_S)
        for bad in (b"", b"\x30\x00", good[:40], good + b"\0", der_signature(0, 1), der_signature(1, vault.P256_N)):
            self.assertFalse(vault.verify_ecdsa(b"sample", bad, RFC_KEY))

    def test_rejects_key_off_curve(self):
        self.assertFalse(vault.verify_ecdsa(b"sample", der_signature(RFC_R, RFC_S), (RFC_KEY[0], RFC_KEY[1] + 1)))

    def test_catalog_is_not_parsed_without_a_valid_signature(self):
        signature = base64.b64encode(der_signature(RFC_R, RFC_S)).decode()
        with mock.patch.object(vault, "catalog_key", return_value=RFC_KEY), self.assertRaises(ValueError):
            vault.verified_catalog(b'{"mods": []}', signature)

    def test_catalog_signed_with_the_configured_key_is_parsed(self):
        signature = base64.b64encode(der_signature(RFC_R, RFC_S)).decode()
        with mock.patch.object(vault, "catalog_key", return_value=RFC_KEY), \
                mock.patch.object(vault, "parse_catalog", return_value=["item"]) as parse, \
                mock.patch.object(vault.json, "loads", return_value={}):
            self.assertEqual(vault.verified_catalog(b"sample", signature), ["item"])
        parse.assert_called_once()


class ConfigTests(unittest.TestCase):
    SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")

    def public_key(self):
        point = b"\x04" + RFC_KEY[0].to_bytes(32, "big") + RFC_KEY[1].to_bytes(32, "big")
        return base64.b64encode(self.SPKI + point).decode()

    def test_parses_a_public_key(self):
        self.assertEqual(vault.parse_public_key(self.public_key()), RFC_KEY)

    def test_rejects_other_keys(self):
        for text in ("", "AAAA", "not base64!"):
            with self.subTest(text), self.assertRaises(ValueError):
                vault.parse_public_key(text)

    def test_reads_dotenv_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('# comment\n\nA=1\nB = "two=2"\nC=\n', encoding="utf-8")
            self.assertEqual(vault.parse_dotenv(path), {"A": "1", "B": "two=2", "C": ""})

    def test_environment_overrides_dotenv(self):
        with mock.patch.object(vault, "parse_dotenv", return_value={"PATCHVAULT_CATALOG_URL": "file"}), \
                mock.patch.dict(vault.os.environ, {"PATCHVAULT_CATALOG_URL": "env", "OTHER": "x"}):
            config = vault.load_config()
        self.assertEqual(config["PATCHVAULT_CATALOG_URL"], "env")
        self.assertNotIn("OTHER", config)

    def test_missing_key_is_reported(self):
        with mock.patch.object(vault, "load_config", return_value={}), self.assertRaises(ValueError):
            vault.catalog_key()

    def test_check_without_urls_reports_an_error(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(vault, "load_config", return_value={}):
            store = vault.Vault(folder)
            store.check()
            self.assertIn("PATCHVAULT_CATALOG_URL", store.check_error)


class CatalogTests(unittest.TestCase):
    def test_rejects_unsafe_entries(self):
        self.assertIsNone(vault.normalize(entry(id="../x")))
        self.assertIsNone(vault.normalize(entry(url="http://example.com/a.zip")))
        self.assertIsNone(vault.normalize(entry(sha256="abc")))

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
