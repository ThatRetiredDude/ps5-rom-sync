"""Known sync targets on a jailbroken PS5.

Paths come from each project's README:
  RetroArch (PPSA99169): https://github.com/bigdaddydev805/PS5_RetroArch
  Porpoise (Dolphin):    https://github.com/elripalda/Porpoise-Dolphin-Emulator-for-PS5
"""

import os
import re

# Where things live on the PS5 differs between setups (another RetroArch title id, a
# Porpoise install under /data/homebrew/...). Override the defaults with environment
# variables; every sync can still be edited by hand in the web page.
DEFAULT_RETROARCH_DIR = "/data/homebrew/PPSA99169"
DEFAULT_PORPOISE_GAMES_DIR = "/data/porpoise/games"
DEFAULT_PORPOISE_COVERS_DIR = "/data/porpoise/covers"


def build_presets(env=None):
    env = os.environ if env is None else env

    def setting(name, default):
        return (env.get(name) or default).rstrip("/") or "/"

    retroarch = setting("RETROARCH_DIR", DEFAULT_RETROARCH_DIR)
    porpoise_games = setting("PORPOISE_GAMES_DIR", DEFAULT_PORPOISE_GAMES_DIR)
    return {
        "retroarch": {
            "label": "RetroArch (ROMs)",
            "remote_base": f"{retroarch}/content",
            "needs_subfolder": True,
            "extensions": [],
            "help": "ROMs go in content/<system>. RetroArch sees this as /app0/content.",
        },
        "retroarch-bios": {
            "label": "RetroArch (BIOS / system files)",
            "remote_base": f"{retroarch}/system",
            "needs_subfolder": False,
            "extensions": [],
            "help": "BIOS files. PS2 BIOS belongs in system/pcsx2/bios, FBNeo files in system/fbneo.",
        },
        "porpoise": {
            "label": "Porpoise (GameCube / Wii)",
            "remote_base": porpoise_games,
            "needs_subfolder": False,
            "extensions": ["iso", "gcm", "rvz", "ciso", "gcz", "wbfs", "wia"],
            "help": f"Porpoise scans {porpoise_games} up to four folders deep. "
                    "Check the path against your PS5: it differs between installs.",
        },
        "porpoise-covers": {
            "label": "Porpoise (cover art)",
            "remote_base": setting("PORPOISE_COVERS_DIR", DEFAULT_PORPOISE_COVERS_DIR),
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


PRESETS = build_presets()

# Common RetroArch system folder names, offered as suggestions in the UI.
RETROARCH_SYSTEMS = [
    "nes", "snes", "n64", "gb", "gbc", "gba", "nds", "genesis", "mastersystem",
    "gamegear", "segacd", "sega32x", "saturn", "dreamcast", "psx", "ps2", "psp",
    "pcengine", "neogeo", "arcade", "fbneo", "mame", "atari2600", "atari7800",
    "lynx", "wonderswan", "ngp", "virtualboy", "dos", "scummvm", "gc", "wii",
]

# Library folders and PS5 folders for the same console go by many names (RomM uses slugs
# like "genesis-slash-megadrive", people name PS5 folders "gameboycolor" or "Saturn").
# Each line is one console: the first name is what we suggest when the PS5 has no folder
# for it yet, the rest are names seen in the wild. Compared lowercase, letters and digits only.
SYSTEM_ALIASES = [
    ("nes", "famicom", "fc", "nintendoentertainmentsystem"),
    ("snes", "sfc", "sfam", "superfamicom", "supernintendo", "supernintendoentertainmentsystem"),
    ("n64", "nintendo64"),
    ("gb", "gameboy"),
    ("gbc", "gameboycolor", "gbcolor"),
    ("gba", "gameboyadvance"),
    ("nds", "ds", "nintendods"),
    ("gc", "ngc", "gamecube", "nintendogamecube"),
    ("wii", "nintendowii"),
    ("genesis", "megadrive", "md", "segagenesis", "segamegadrive", "genesisslashmegadrive",
     "genesismegadrive", "megadrivegenesis"),
    ("mastersystem", "sms", "segamastersystem"),
    ("gamegear", "gg", "segagamegear"),
    ("segacd", "megacd", "segamegacd"),
    ("sega32x", "32x", "sega32", "genesis32x"),
    ("saturn", "ss", "segasaturn"),
    ("dreamcast", "dc", "segadreamcast"),
    ("psx", "ps", "ps1", "psone", "playstation", "sonyplaystation"),
    ("ps2", "playstation2", "sonyplaystation2"),
    ("psp", "playstationportable", "sonyplaystationportable"),
    ("pcengine", "pce", "tg16", "turbografx16", "turbografx", "pcenginecd", "turbografxcd"),
    ("neogeo", "neogeoaes", "neogeomvs", "neogeocd"),
    ("arcade", "arcadegames"),
    ("atari2600", "a2600", "atari26"),
    ("atari7800", "a7800"),
    ("lynx", "atarilynx"),
    ("wonderswan", "ws", "wsc", "wonderswancolor"),
    ("ngp", "ngpc", "neogeopocket", "neogeopocketcolor"),
    ("virtualboy", "vb"),
    ("dos", "msdos"),
    ("scummvm",),
]
_ALIAS_GROUP = {}
for _group in SYSTEM_ALIASES:
    for _name in _group:
        _ALIAS_GROUP.setdefault(_name, _group[0])


def _squash(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def guess_system(folder, existing=()):
    """Guess the RetroArch content folder for a library folder name.

    `existing` are the folder names already on the PS5 under content/. When one of them
    is the same console, its exact spelling wins (so "dc" finds an existing "dreamcast"
    and "saturn" finds "Saturn"). Returns None when nothing matches.
    """
    key = _squash(folder.strip("/").split("/")[-1])
    if not key:
        return None
    group = _ALIAS_GROUP.get(key)
    for name in sorted(existing):
        squashed = _squash(name)
        if squashed == key or (group and _ALIAS_GROUP.get(squashed) == group):
            return name
    return group


def remote_path_for(target: str, subfolder: str = "") -> str:
    preset = PRESETS.get(target, PRESETS["custom"])
    base = preset["remote_base"].rstrip("/")
    sub = subfolder.strip("/")
    return f"{base}/{sub}" if sub else base
