"""Reads platforms, collections and ROM files from a RomM server (https://github.com/rommapp/romm).

Only the API is used to find out *which* files belong to a platform or collection. The files
themselves are read from the library mount, so RomM's library folder must be mounted at
LIBRARY_ROOT (the same folder RomM has at /romm/library).
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from .presets import remote_path_for

PAGE_SIZE = 500

# RomM platform slug -> (target preset, RetroArch system folder). RomM uses IGDB-style slugs;
# a few systems have more than one spelling, so aliases are listed too.
ROMM_PLATFORMS = {
    "nes": ("retroarch", "nes"), "famicom": ("retroarch", "nes"),
    "snes": ("retroarch", "snes"), "sfam": ("retroarch", "snes"),
    "n64": ("retroarch", "n64"),
    "gb": ("retroarch", "gb"), "gbc": ("retroarch", "gbc"), "gba": ("retroarch", "gba"),
    "nds": ("retroarch", "nds"),
    "virtualboy": ("retroarch", "virtualboy"),
    "genesis-slash-megadrive": ("retroarch", "genesis"), "genesis": ("retroarch", "genesis"),
    "megadrive": ("retroarch", "genesis"),
    "sms": ("retroarch", "mastersystem"), "mastersystem": ("retroarch", "mastersystem"),
    "gamegear": ("retroarch", "gamegear"), "gg": ("retroarch", "gamegear"),
    "segacd": ("retroarch", "segacd"), "sega32": ("retroarch", "sega32x"),
    "sega32x": ("retroarch", "sega32x"),
    "saturn": ("retroarch", "saturn"), "dc": ("retroarch", "dreamcast"),
    "dreamcast": ("retroarch", "dreamcast"),
    "psx": ("retroarch", "psx"), "ps": ("retroarch", "psx"),
    "ps2": ("retroarch", "ps2"), "psp": ("retroarch", "psp"),
    "tg16": ("retroarch", "pcengine"), "turbografx16--1": ("retroarch", "pcengine"),
    "pce": ("retroarch", "pcengine"), "pcengine": ("retroarch", "pcengine"),
    "neogeoaes": ("retroarch", "neogeo"), "neogeomvs": ("retroarch", "neogeo"),
    "neo-geo": ("retroarch", "neogeo"), "neogeo": ("retroarch", "neogeo"),
    "neo-geo-pocket": ("retroarch", "ngp"), "neo-geo-pocket-color": ("retroarch", "ngp"),
    "arcade": ("retroarch", "arcade"), "fbneo": ("retroarch", "fbneo"),
    "mame": ("retroarch", "mame"),
    "atari2600": ("retroarch", "atari2600"), "atari7800": ("retroarch", "atari7800"),
    "lynx": ("retroarch", "lynx"),
    "wonderswan": ("retroarch", "wonderswan"), "wonderswan-color": ("retroarch", "wonderswan"),
    "dos": ("retroarch", "dos"), "scummvm": ("retroarch", "scummvm"),
    "ngc": ("porpoise", ""), "gamecube": ("porpoise", ""), "gc": ("porpoise", ""),
    "wii": ("porpoise", ""),
}


def suggest_target(slug):
    """Best PS5 target for a RomM platform slug, or None when this repo doesn't know it."""
    hit = ROMM_PLATFORMS.get((slug or "").lower())
    if not hit:
        return None
    target, subfolder = hit
    return {"target": target, "subfolder": subfolder,
            "remote_path": remote_path_for(target, subfolder)}


class RommError(Exception):
    pass


class RommClient:
    def __init__(self, settings, timeout=15):
        self.url = (settings.get("url") or "").strip().rstrip("/")
        if not self.url:
            raise RommError("Set your RomM address first")
        self.headers = {"Accept": "application/json"}
        token = (settings.get("token") or "").strip()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        elif settings.get("user"):
            raw = f"{settings['user']}:{settings.get('password') or ''}".encode()
            self.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        self.timeout = timeout

    def _get(self, path, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{self.url}{path}{'?' + query if query else ''}",
                                     headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RommError("RomM rejected the login. Check the user and password or API token.")
            raise RommError(f"RomM returned {e.code} for {path}")
        except (urllib.error.URLError, OSError) as e:
            raise RommError(f"Could not reach RomM at {self.url}: {getattr(e, 'reason', e)}")

    def platforms(self):
        return [p for p in _items(self._get("/api/platforms")) if p.get("rom_count", 1)]

    def collections(self):
        return _items(self._get("/api/collections"))

    def roms(self, platform_id=None, collection_id=None):
        """All ROMs in a platform or collection, following pagination when the server uses it."""
        out, offset = [], 0
        while True:
            page = self._get("/api/roms", platform_id=platform_id, collection_id=collection_id,
                             limit=PAGE_SIZE, offset=offset)
            items = _items(page)
            out.extend(items)
            # Older RomM versions return a plain list with everything in it.
            if not isinstance(page, dict) or len(items) < PAGE_SIZE:
                return out
            offset += len(items)
            if offset >= page.get("total", offset + 1):
                return out


def _items(data):
    return data.get("items", []) if isinstance(data, dict) else list(data or [])


def rom_relpath(rom):
    """A ROM's file or folder relative to RomM's library root, e.g. 'roms/snes/Chrono.sfc'."""
    fs_path = (rom.get("fs_path") or "").strip("/")
    # Some RomM versions report fs_path with the library folder in front.
    for prefix in ("romm/library/", "library/"):
        if fs_path.startswith(prefix):
            fs_path = fs_path[len(prefix):]
    return f"{fs_path}/{rom['fs_name']}" if fs_path else rom["fs_name"]


def rom_platform_slug(rom):
    return rom.get("platform_slug") or rom.get("platform_fs_slug") or ""


def test_connection(settings):
    client = RommClient(settings, timeout=8)
    platforms = client.platforms()
    return {"ok": True, "platforms": len(platforms)}
