# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make run     # open the app from source (needs Tkinter)
make check   # print available updates to the terminal, no GUI
make test    # run the tests
make build   # build dist/PatchVault-<platform>-<arch>.zip for the current OS
make clean   # remove dist/, .venv/ and caches (.data/ survives)
```

Run a single test class or method:

```bash
python3 -B -m unittest tests.test_vault.RepackTests
python3 -B -m unittest tests.test_vault.RepackTests.<test_method>
```

There is no linter or formatter configured. `make build` exits with an error unless
`PATCHVAULT_RELEASES_URL` is set in `.env` or the environment.

## Architecture

The whole app is one file, `patchvault.py`, which PyInstaller packages directly. It runs on the
standard library alone: Tkinter and Pillow are imported optionally, and `certifi` is only used
in packaged builds. Keep source compatible with Python 3.8. `tests/test_vault.py` imports the module
as `vault` and `scripts/build.py` imports it for its constants, so neither may trigger GUI startup.

The file is layered top to bottom:

- **Catalog parsing** (`normalize`, `parse_catalog`, `find_rows`, `FIELD_ALIASES`, `ROOT_ALIASES`).
  Catalogs are untrusted remote JSON with no fixed schema. Fields are found through aliases or a
  per-catalog user mapping. Only an id and one HTTPS URL are required; SHA-256 and size may be
  absent, and the code degrades rather than fails (`status_of` falls back to version comparison,
  `_download` accepts an unverified file).
- **Repackaging** (`plan_repack`, `repack`). Rewrites a downloaded zip into the layout described in
  the README, enforcing limits such as `MAX_UNPACKED`.
- **`Vault`**. All state and logic, with no Tkinter dependency, which is what the tests exercise.
- **`App`, `GameRow`, `CatalogManager`**. The Tkinter UI.

### Threading

`main` starts `WORKERS` download threads running `Vault.worker` and a `periodic_check` thread. UI
actions spawn daemon threads for network work. The UI never receives callbacks from them: `App.refresh`
re-reads `Vault.snapshot()` every 400 ms and repaints. To surface new state, add it to `snapshot()`
and paint it in `refresh`. Shared `Vault` state is guarded by `self.lock`, an `RLock`, and network
calls happen outside it, so code re-checks state afterwards (see `check`, which ignores a result if
the active catalog changed while loading).

### Per-catalog state

`Vault._use_catalog` repoints `root`, `art_dir`, `state_path` and `cache_path` at the active
catalog's folder, so those attributes change at runtime. Workers compute targets from `self.root`,
which is why adding a catalog is refused while `busy()`.

Data lives under `base_dir()`: `.data/` from source, next to the program when packaged, or
`--dir` if given.

- `downloaded/<catalog>/`, `covers/<catalog>/`: user-visible output.
- `.catalogs/index.json`: the catalog registry and the active catalog.
- `.catalogs/<catalog>.json`, `.state.json`, `.covers.json`: cached catalog, download records, art index.
- `.parts/`: resumable partial downloads, keyed by id and SHA-256 prefix.

A state record stores the SHA-256 of the *original* download, not of the repacked zip. That is how
updates are detected after repackaging.

### Configuration and self-update

`load_config` merges build-time values, then `.env` (source runs only), then the real environment.
`scripts/build.py` embeds `CONFIG_KEYS` and `PATCHVAULT_VERSION` into a generated
`patchvault_build_config` module, so a new config key belongs in `CONFIG_KEYS`. Source runs have no
version, so `check_app_update` returns `"unversioned"` and the self-update path cannot be exercised
without a packaged build.

`asset_name()` is shared: `scripts/build.py` names the zip with it and `pick_asset` matches release
assets by it, so renaming it strands installed builds, which look for the old name. The `publish` job in `.github/workflows/build.yml`
expects exactly 4 assets, so a change to the build matrix must also change `EXPECTED`.

## Conventions

- User-facing strings, including error messages and CLI help, are in Vietnamese. Identifiers,
  comments and docs are in English.
- Commit messages must follow Conventional Commits: pushing to `main` runs semantic-release, so
  `fix:` and `feat:` publish a release to users, and `feat!:` is a major release. Use `docs:`,
  `chore:`, `refactor:` or `test:` for changes that should not release.
