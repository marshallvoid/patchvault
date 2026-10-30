# PatchVault

A desktop app that downloads, tracks and repackages Vietnamese translation patches for Nintendo
Switch games from remote catalogs that you add yourself.

## Features

- Add several catalogs by URL and switch between them from the app.
- Verifies the SHA-256 of every download, resumes interrupted transfers and falls back to mirrors.
- Detects updates, even after a patch has been repackaged.
- Updates itself from GitHub releases.
- Repackages every patch into one consistent layout:

```
downloaded/
└── <catalog>/
    └── [VI] - <Game name>/
        └── [<game version>-unreviewed] [VI].zip
            └── contents/<title id>/...
```

Games with no version in the catalog use `miss`. Files outside the title ID folder, such as
instructions, are not kept in the new zip.

Catalogs are not signed, so a catalog decides what you download. Only add catalogs you trust. No
catalog is included with the app.

## Install

Download the zip for your platform from [Releases](../../releases) and extract it anywhere. It contains:

```
PatchVault          # .exe on Windows, .app on macOS
downloaded/         # finished patches
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
