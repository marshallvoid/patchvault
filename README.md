# PatchVault

A desktop app that downloads, tracks and repackages Vietnamese translation patches for Nintendo
Switch games from a signed remote catalog.

## Features

- Uses a catalog only when its detached ECDSA P-256 signature is valid.
- Verifies the SHA-256 of every download, resumes interrupted transfers and falls back to mirrors.
- Detects updates, even after a patch has been repackaged.
- Repackages every patch into one consistent layout:

```
downloaded/
└── [VI] - <Game name>/
    └── [<game version>-unreviewed] [VI].zip
        └── contents/<title id>/...
```

Games with no version in the catalog use `miss`. Files outside the title ID folder, such as
instructions, are not kept in the new zip.

## Install

Download the zip for your platform from Releases and extract it anywhere. It contains:

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

## Configuration

The catalog location and signing key are not stored in this repository.

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `PATCHVAULT_CATALOG_URL` | HTTPS URL of `catalog.json` |
| `PATCHVAULT_SIGNATURE_URL` | HTTPS URL of the catalog signature |
| `PATCHVAULT_PUBLIC_KEY` | P-256 public key, the base64 body of a PEM file on one line |

Real environment variables override `.env`. Packaged builds embed these values at build
time. Download URLs come from the catalog itself.

When running from source, downloads, covers and caches live in `.data/` at the project root, which
is ignored by git and survives `make clean`.

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

When a release is published, the workflow builds Windows, macOS (Apple Silicon and Intel) and Linux
zips and attaches them to it. Add `PATCHVAULT_CATALOG_URL`, `PATCHVAULT_SIGNATURE_URL` and
`PATCHVAULT_PUBLIC_KEY` as repository secrets first. The build can also be started by hand from the
Actions tab, in which case the zips are stored as workflow artifacts.
