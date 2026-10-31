# PatchVault

A desktop app that downloads, tracks and repackages game mods from remote catalogs that you add
yourself, and tidies the mods you already have into the same layout. It works with any mod,
whatever its language or purpose.

## Features

- Add several catalogs by URL and switch between them from the app.
- Pick several games, or all of them, and stop every download at once.
- Resumes interrupted transfers, falls back to mirrors and verifies the SHA-256 when the catalog
  has one.
- Detects updates, even after a mod has been repackaged.
- Updates itself from GitHub releases.
- Repackages every mod into one consistent layout, with Deflate at level 9:

```
downloaded/
└── <catalog>/
    └── <Game name> - [<mod name>]/
        └── [<game version>-unreviewed] [<mod name>].zip
            └── contents/<title id>/...
```

Catalogs are not signed, so a catalog decides what you download. Only add catalogs you trust. No
catalog is included with the app.

## Naming

You enter the mod name once at the top of the window, and it is required before downloading or
normalizing. It can be anything, such as the language or the author of the mod. When normalizing, a
source that already carries a name keeps it, as in `[1.0.0] [60FPS].zip` or a `[60FPS] - Game` folder,
so different mods of one game stay apart.

Game names come from [titledb](https://github.com/blawar/titledb), then from a catalog listing the
title ID, and when normalizing, from the file name. The database is about 90 MB, downloaded once
and refreshed weekly. In paths and in the app, game names keep only letters without accents, digits,
spaces and hyphens, and all-capitals names become Title Case. A game with no known version uses `miss`.

Only the title ID folders are kept, so instructions and other files around them are dropped. A
pack with several title IDs, such as a game and a system font, stays one zip named after the game.

## Downloading

Choose **Thư mục đích** in the **Tải về** tab to save mods as `<folder>/<catalog>/...` instead of
`downloaded/<catalog>/...`. The folder is remembered between runs.

A game counts as downloaded when its `<Game name> - [<mod name>]` folder in the download folder in
use holds a zip, so files moved in by hand work and other folders do not count. In the list,
Ctrl+click, Shift+click and Ctrl+A pick several games for **Tải đã chọn**.

## Normalizing existing mods

The **Chuẩn hóa** tab takes a source folder and a destination folder. The source can have any
structure: the app finds every zip and every folder of title IDs in it. **Quét** only shows what
would happen, and **Chuẩn hóa** then saves each mod as:

```
<destination>/<Game name> - [<mod name>]/[<game version>-<internal version>] [<mod name>].zip
```

The internal version comes from a source named like `[1.1.1-v262144]`, otherwise it is `unreviewed`.

Originals are moved to `<destination>/backup/`, keeping their relative paths. Once every mod in a
top-level folder of the source is saved, that whole folder moves, including anything else in it. If
a mod in it failed or was skipped, only the saved mods move. Existing files are never overwritten. The destination can contain the source, but not the other way round.

## Install

Download the zip for your platform from [Releases](../../releases) and extract it anywhere. It contains:

```
PatchVault          # .exe on Windows, .app on macOS
downloaded/         # finished mods
covers/             # cached artwork
```

The app keeps its data next to itself, so the whole folder can be moved or copied.

| Platform | Notes |
|---|---|
| Windows | Not code signed, so SmartScreen may warn on first launch |
| macOS | Not notarized, so Gatekeeper blocks the first launch |
| Linux | Built on Ubuntu 22.04, needs glibc 2.35 or newer |

On macOS, a quarantined app stores its data in `~/Library/Application Support/PatchVault`. To
keep the data next to the app, clear the quarantine flag:

```bash
xattr -dr com.apple.quarantine PatchVault.app
```

## Catalogs

Choose **Quản lý** in the app and enter a name and an HTTPS URL. Each catalog keeps its own
downloads, artwork and state, so two catalogs can list the same game. Removing a catalog keeps its
downloaded files. A catalog is a JSON file:

```json
{
  "mods": [
    {
      "id": "game",
      "title_id": "0100000000000000",
      "name": "Game name",
      "game_version": "1.0.0",
      "mod_version": "v1",
      "sha256": "<SHA-256 of the zip>",
      "size": 12345,
      "url": "https://example.com/game.zip"
    }
  ]
}
```

Optional fields are `tagline`, `developer`, `release`, `translator`, `tags`, `description`,
`highlights`, `done`, `kept`, `changelog`, `credit`, `cover`, `background` and `mirrors`.

## Configuration

Local builds read `.env`, see `.env.example`. Real environment variables override it.

| Variable | Description |
|---|---|
| `PATCHVAULT_RELEASES_URL` | GitHub API URL of the latest release, for app update checks |

The value is embedded at build time. When running from source, downloads, covers and caches live
in `.data/` at the project root, which is ignored by git and survives `make clean`.

## Development

Requires Python 3.8+ with Tkinter. Pillow is optional and enables artwork.

```bash
make run     # open the app from source
make check   # print available updates
make test    # run the tests
make build   # create dist/PatchVault-<platform>-<arch>.zip
make clean   # remove build output and caches
```

## Releases

Pushing to `main` runs [semantic-release](https://semantic-release.gitbook.io/), which reads
[Conventional Commits](https://www.conventionalcommits.org/) to choose the next version:

| Commit | Release |
|---|---|
| `fix: ...` | Patch |
| `feat: ...` | Minor |
| `feat!: ...` or a `BREAKING CHANGE:` footer | Major |
| `docs:`, `chore:`, `refactor:`, `test:`, ... | None |

semantic-release creates a draft, the workflow builds Windows, macOS (Apple Silicon and Intel) and
Linux zips and attaches them, then publishes the release once all four are there. The app therefore
never sees a release without its zips. If a build fails, the draft stays unpublished. The build can
also be started by hand from the Actions tab, in which case the zips are stored as workflow
artifacts.

Add `PATCHVAULT_RELEASES_URL` as a repository secret, see Configuration.

## Updates

Packaged builds check the latest GitHub release at startup and every 30 minutes. When the user
accepts an update, the app downloads the zip for its platform, verifies it against the SHA-256
reported by GitHub, replaces only the program and restarts. `downloaded/` and `covers/` are not
touched. If the program folder is not writable, or the release has no zip for the platform, the app
opens the release page instead. Builds without a version, such as local ones, skip this check.
