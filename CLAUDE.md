# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make run     # open the app from source (needs Tkinter)
make check   # print available updates to the terminal, no GUI
make test    # run the tests
make lint    # ruff check, ruff format --check and mypy
make format  # ruff --fix and ruff format
make build   # build dist/PatchVault-<platform>-<arch>.zip for the current OS
make clean   # remove dist/, .venv/ and caches (.data/ survives)
```

Run a single test class or method:

```bash
python3 -B -m unittest tests.test_vault.RepackTests
python3 -B -m unittest tests.test_vault.RepackTests.<test_method>
```

Settings are in `pyproject.toml`; CI runs the same checks, and a failure blocks a release. `make install`
also installs the pre-commit hook. `make build` exits with an error unless
`PATCHVAULT_RELEASES_URL` is set in `.env` or the environment.

## Architecture

`main.py` is the entry point and the script PyInstaller packages; `python3 -m patchvault` also works.
The app runs on the standard library alone: Tkinter and Pillow are optional, and `certifi` is only
used in packaged builds. Keep source compatible with Python 3.8.

```
patchvault/
├── config.py       constants, .env and build config, data folder
├── net.py          http_get
├── system.py       per-OS code, replacing the running program
├── names.py        game name cleaning, versions, normalized_path
├── catalog.py      parsing untrusted catalogs
├── zips.py         writing the shared zip layout
├── normalizer.py   finding mods on disk, planning what to do with them
├── vault.py        Vault: all state and logic
├── cli.py          main, the background check loop
└── ui/             app.py, normalize_tab.py, catalog_manager.py, game_row.py, widgets.py, ...
```

Each module depends only on those listed above it. Only `cli.main` imports `ui`, inside the
function, so `--check` and the tests work without Tkinter.

`http_get`, `load_config`, `parse_dotenv`, `program_path` and `repack` are called through their
module (`net.http_get(...)`, `settings.load_config()`) because the tests patch them there.
`config` is imported as `settings` where a local variable is also called `config`.

- **Catalogs** (`catalog.py`). Catalogs have no fixed schema. Fields are found through aliases or a
  per-catalog user mapping. Only an id and one HTTPS URL are required; SHA-256 and size may be
  absent, and the code degrades rather than fails (`status_of` falls back to version comparison,
  `_download` accepts an unverified file).
- **Repackaging** (`zips.py`). `plan_repack` and `repack` rewrite a zip into the layout described in
  the README, keeping every title ID folder and enforcing limits such as `MAX_UNPACKED`.
- **Normalizing** (`normalizer.py`, `Vault.scan_mods`, `Vault.normalize_mods`). Planning never
  touches the disk, so the UI can show a review first. Originals move to `<destination>/backup`
  only after they are saved, a top-level source folder only after every mod in it is saved, and
  targets are never overwritten. Game names come from titledb (`Vault.title_names`), cached in
  `.catalogs/_titles.json`. A source's own `[tag]` is its mod name (`source_mod_name`).
- **Game names**. `Vault._named` replaces each item's `name` with the titledb name and keeps the
  original as `catalog_name`, so anything using `name` gets the official one.
- **UI**. Both tabs show their log through `LogPane` and `ViewSwitch`; logs live in `Vault` and last
  for the session.

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

`config.load_config` merges build-time values, then `.env` (source runs only), then the real environment.
`scripts/build.py` embeds `CONFIG_KEYS` and `PATCHVAULT_VERSION` into a generated
`patchvault_build_config` module, so a new config key belongs in `CONFIG_KEYS`. Source runs have no
version, so `check_app_update` returns `"unversioned"` and the self-update path cannot be exercised
without a packaged build.

`asset_name()` is shared: `scripts/build.py` names the zip with it and `pick_asset` matches release
assets by it, so renaming it strands installed builds, which look for the old name. The `publish` job in `.github/workflows/build.yml`
expects exactly 4 assets, so a change to the build matrix must also change `EXPECTED`.

## Conventions

- No docstrings anywhere, on files, classes or functions, including in new files. Use a short `#`
  comment for code that is complex or needs a reason.
- User-facing strings, including error messages and CLI help, are in Vietnamese. Identifiers,
  comments and docs are in English.
- Commit messages must follow Conventional Commits: pushing to `main` runs semantic-release, so
  `fix:` and `feat:` publish a release to users, and `feat!:` is a major release. Use `docs:`,
  `chore:`, `refactor:` or `test:` for changes that should not release.
