"""Known sync targets on a jailbroken PS5.

Paths come from each project's README:
  RetroArch (PPSA99169): https://github.com/bigdaddydev805/PS5_RetroArch
  Porpoise (Dolphin):    https://github.com/elripalda/Porpoise-Dolphin-Emulator-for-PS5
"""

RETROARCH_ROOT = "/data/homebrew/PPSA99169"

PRESETS = {
    "retroarch": {
        "label": "RetroArch (ROMs)",
        "remote_base": f"{RETROARCH_ROOT}/content",
        "needs_subfolder": True,
        "extensions": [],
        "help": "ROMs go in content/<system>. RetroArch sees this as /app0/content.",
    },
    "retroarch-bios": {
        "label": "RetroArch (BIOS / system files)",
        "remote_base": f"{RETROARCH_ROOT}/system",
        "needs_subfolder": False,
        "extensions": [],
        "help": "BIOS files. PS2 BIOS belongs in system/pcsx2/bios, FBNeo files in system/fbneo.",
    },
    "porpoise": {
        "label": "Porpoise (GameCube / Wii)",
        "remote_base": "/data/porpoise/games",
        "needs_subfolder": False,
        "extensions": ["iso", "gcm", "rvz", "ciso", "gcz", "wbfs", "wia"],
        "help": "Porpoise scans /data/porpoise/games up to four folders deep.",
    },
    "porpoise-covers": {
        "label": "Porpoise (cover art)",
        "remote_base": "/data/porpoise/covers",
        "needs_subfolder": False,
        "extensions": ["png"],
        "help": "PNG files named by disc ID, e.g. GALE01.png.",
    },
    "custom": {
        "label": "Custom path",
        "remote_base": "/data/roms",
        "needs_subfolder": False,
        "extensions": [],
        "help": "Any folder on the PS5.",
    },
}

# Common RetroArch system folder names, offered as suggestions in the UI.
RETROARCH_SYSTEMS = [
    "nes", "snes", "n64", "gb", "gbc", "gba", "nds", "genesis", "mastersystem",
    "gamegear", "segacd", "sega32x", "saturn", "dreamcast", "psx", "ps2", "psp",
    "pcengine", "neogeo", "arcade", "fbneo", "mame", "atari2600", "atari7800",
    "lynx", "wonderswan", "ngp", "virtualboy", "dos", "scummvm",
]


def remote_path_for(target: str, subfolder: str = "") -> str:
    preset = PRESETS.get(target, PRESETS["custom"])
    base = preset["remote_base"].rstrip("/")
    sub = subfolder.strip("/")
    return f"{base}/{sub}" if sub else base
