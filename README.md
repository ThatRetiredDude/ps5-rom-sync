# PS5 ROM Sync

A small Docker container with a web page that copies your ROM library to a jailbroken PS5 over FTP. Point it at your PS5's IP, hit **+** to add a library, pick where it goes (RetroArch, Porpoise, or any folder), and hit **Sync now**. Files that are already on the PS5 with the same size are skipped, so re-syncing is fast.

## What it can sync to

| Target | Folder on the PS5 | Notes |
|---|---|---|
| RetroArch (ROMs) | `/data/homebrew/PPSA99169/content/<system>` | RetroArch sees this as `/app0/content`. Pick a system folder such as `snes` or `psx`. |
| RetroArch (BIOS / system files) | `/data/homebrew/PPSA99169/system` | PS2 BIOS goes in `system/pcsx2/bios`, FBNeo files in `system/fbneo`. |
| Porpoise (GameCube / Wii) | `/data/porpoise/games` | Only `iso gcm rvz ciso gcz wbfs wia` by default. Porpoise scans four folders deep, so subfolders are fine. |
| Porpoise (cover art) | `/data/porpoise/covers` | PNGs named by disc ID, such as `GALE01.png`. |
| Custom path | anything you type | For other emulators or homebrew. |

The paths come from the [PS5 RetroArch port](https://github.com/bigdaddydev805/PS5_RetroArch) (title ID `PPSA99169`) and the [Porpoise README](https://github.com/elripalda/Porpoise-Dolphin-Emulator-for-PS5). Every target's PS5 folder can be edited if your install is different.

## Before you start

The PS5 needs an FTP server running:

* **ftpsrv** payload: port **2121** (the default here)
* **etaHEN** built-in FTP: port **1337**

The login is usually anonymous; leave the user as `anonymous` and the password blank unless you set one.

## Install

```yaml
# docker-compose.yml
services:
  ps5-rom-sync:
    image: ghcr.io/thatretireddude/ps5-rom-sync:latest
    container_name: ps5-rom-sync
    ports:
      - "8585:8585"
    volumes:
      - ./config:/config
      - /path/to/your/roms:/roms:ro
    restart: unless-stopped
```

```sh
docker compose up -d
```

Open `http://<your-server>:8585`, enter the PS5's IP address, hit **Save**, then **Test**.

Or with plain Docker:

```sh
docker run -d --name ps5-rom-sync -p 8585:8585 \
  -v $PWD/config:/config -v /path/to/your/roms:/roms:ro \
  ghcr.io/thatretireddude/ps5-rom-sync:latest
```

To build it yourself: `docker build -t ps5-rom-sync .`

## Using it

1. Hit **+**. Choose **RetroArch (ROMs)**, browse to your SNES folder, and set the system folder to `snes`.
2. Hit **+** again. Choose **Porpoise**, and browse to your GameCube/Wii folder.
3. Hit **+** again for anything else, such as a custom folder for another emulator.
4. Hit **Sync now** on a card, or **Sync all** at the top.

Each sync can also:

* **Only copy certain file types.** Blank means everything.
* **Auto-sync every N minutes.** 0 turns it off. Handy if the PS5 is on a lot.
* **Delete files on the PS5 that are no longer in the library.** Off by default. It only removes files of the chosen types inside that sync's PS5 folder.

Syncs run one at a time, because PS5 FTP servers don't like several uploads at once. Each file is uploaded as `name.part` and renamed when it finishes, so an interrupted sync never leaves a half-copied ROM with the real name.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `WEB_PORT` | `8585` | Port for the web page |
| `PS5_HOST` | (blank) | Pre-fills the PS5 IP |
| `PS5_FTP_PORT` | `2121` | Pre-fills the FTP port |
| `LIBRARY_ROOT` | `/roms` | Where the library is mounted in the container |
| `CONFIG_DIR` | `/config` | Where `config.json` is saved |

## Development

```sh
pip install -r requirements-dev.txt
python -m pytest -q          # runs against a local fake FTP server
CONFIG_DIR=./config LIBRARY_ROOT=/path/to/roms python -m app.main
```
