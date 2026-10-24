import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import romm
from app.config_store import ConfigStore
from app.main import create_app
from app.sync_engine import JobRun, run_sync

# The library uses RomM's default layout: roms/<platform>/<game>.
PLATFORMS = [
    {"id": 1, "slug": "snes", "fs_slug": "snes", "name": "Super Nintendo", "rom_count": 2},
    {"id": 2, "slug": "ngc", "fs_slug": "gc", "name": "GameCube", "rom_count": 1},
    {"id": 3, "slug": "3do", "fs_slug": "3do", "name": "3DO", "rom_count": 1},
    {"id": 4, "slug": "wii", "fs_slug": "wii", "name": "Wii", "rom_count": 0},
]
ROMS = [
    {"id": 10, "platform_id": 1, "platform_slug": "snes", "fs_path": "roms/snes",
     "fs_name": "Chrono Trigger.sfc"},
    {"id": 11, "platform_id": 1, "platform_slug": "snes", "fs_path": "roms/snes",
     "fs_name": "Earthbound"},  # multi-file game: a folder
    {"id": 20, "platform_id": 2, "platform_slug": "ngc", "fs_path": "roms/gc",
     "fs_name": "Melee.rvz"},
    {"id": 30, "platform_id": 3, "platform_slug": "3do", "fs_path": "roms/3do",
     "fs_name": "Gex.iso"},
]
COLLECTIONS = [{"id": 7, "name": "Favorites", "rom_ids": [10, 20, 30]}]
AUTH = "Basic " + base64.b64encode(b"jared:hunter2").decode()


@pytest.fixture
def romm_server():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            seen.append((url.path, q))
            if self.headers.get("Authorization") != AUTH:
                return self._send(401, {"detail": "nope"})
            if url.path == "/api/platforms":
                return self._send(200, PLATFORMS)
            if url.path == "/api/collections":
                return self._send(200, COLLECTIONS)
            if url.path == "/api/roms":
                roms = ROMS
                if "platform_id" in q:
                    roms = [r for r in roms if r["platform_id"] == int(q["platform_id"])]
                if "collection_id" in q:
                    ids = COLLECTIONS[0]["rom_ids"]
                    roms = [r for r in roms if r["id"] in ids]
                # Page one at a time so pagination gets exercised.
                offset = int(q.get("offset", 0))
                return self._send(200, {"items": roms[offset:offset + 1], "total": len(roms)})
            self._send(404, {})

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_port}/", "user": "jared",
           "password": "hunter2", "token": "", "seen": seen}
    server.shutdown()


@pytest.fixture
def romm_library(tmp_path, monkeypatch):
    lib = tmp_path / "library"
    (lib / "roms" / "snes" / "Earthbound").mkdir(parents=True)
    (lib / "roms" / "snes" / "Chrono Trigger.sfc").write_bytes(b"a" * 100)
    (lib / "roms" / "snes" / "Earthbound" / "Earthbound.sfc").write_bytes(b"b" * 200)
    (lib / "roms" / "snes" / "not-in-romm.sfc").write_bytes(b"x")
    (lib / "roms" / "gc").mkdir(parents=True)
    (lib / "roms" / "gc" / "Melee.rvz").write_bytes(b"c" * 300)
    (lib / "roms" / "3do").mkdir(parents=True)
    (lib / "roms" / "3do" / "Gex.iso").write_bytes(b"d")
    from app import sync_engine
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(lib))
    return lib


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(romm, "PAGE_SIZE", 1)


def test_suggest_target():
    assert romm.suggest_target("snes")["remote_path"] == "/data/homebrew/PPSA99169/content/snes"
    assert romm.suggest_target("genesis-slash-megadrive")["subfolder"] == "genesis"
    assert romm.suggest_target("ngc")["remote_path"] == "/data/porpoise/games"
    assert romm.suggest_target("3do") is None


def test_rom_relpath_strips_library_prefix():
    assert romm.rom_relpath({"fs_path": "library/roms/gba", "fs_name": "a.gba"}) == "roms/gba/a.gba"
    assert romm.rom_relpath({"fs_path": "snes/roms", "fs_name": "b.sfc"}) == "snes/roms/b.sfc"


def test_bad_login(romm_server):
    with pytest.raises(romm.RommError, match="rejected"):
        romm.RommClient({**romm_server, "password": "wrong"}).platforms()


def test_platform_job_syncs_only_romm_files(ftp_server, romm_server, romm_library):
    job = {"name": "SNES", "source": "", "remote_path": "/data/homebrew/PPSA99169/content/snes",
           "extensions": [], "mirror_deletes": True, "romm_platform_id": 1}
    dest = ftp_server["root"] / "data/homebrew/PPSA99169/content/snes"
    dest.mkdir(parents=True)
    (dest / "Removed.sfc").write_bytes(b"old")
    run = JobRun("x")
    run_sync(job, ftp_server, run, romm_settings=romm_server)
    assert (dest / "Chrono Trigger.sfc").stat().st_size == 100
    assert (dest / "Earthbound" / "Earthbound.sfc").stat().st_size == 200
    assert not (dest / "not-in-romm.sfc").exists()
    assert not (dest / "Removed.sfc").exists()
    assert run.files_uploaded == 2 and run.files_deleted == 1
    offsets = [q["offset"] for path, q in romm_server["seen"] if path == "/api/roms"]
    assert offsets == ["0", "1"]


def test_collection_job_routes_each_game(ftp_server, romm_server, romm_library):
    job = {"name": "Favs", "source": "", "remote_path": "/", "extensions": [],
           "mirror_deletes": True, "romm_collection_id": 7}
    run = JobRun("x")
    run_sync(job, ftp_server, run, romm_settings=romm_server)
    root = ftp_server["root"]
    assert (root / "data/homebrew/PPSA99169/content/snes/Chrono Trigger.sfc").exists()
    assert (root / "data/porpoise/games/Melee.rvz").exists()
    assert not list(root.rglob("Gex.iso"))  # 3DO has no PS5 target
    assert run.files_uploaded == 2 and run.files_deleted == 0


def test_wrong_mount_is_explained(ftp_server, romm_server, tmp_path, monkeypatch):
    from app import sync_engine
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(tmp_path / "empty"))
    job = {"name": "SNES", "source": "", "remote_path": "/x", "extensions": [],
           "romm_platform_id": 1}
    with pytest.raises(FileNotFoundError, match="Mount RomM's library"):
        run_sync(job, ftp_server, JobRun("x"), romm_settings=romm_server)


def test_romm_api(romm_server, romm_library, tmp_path):
    store = ConfigStore(str(tmp_path / "config" / "config.json"))
    c = create_app(store, start_manager=False).test_client()
    assert c.get("/api/romm/sources").status_code == 502  # not set up yet
    c.put("/api/romm", json={k: romm_server[k] for k in ("url", "user", "password")})
    assert c.post("/api/romm/test").get_json() == {"ok": True, "platforms": 3}

    sources = c.get("/api/romm/sources").get_json()
    snes = sources["platforms"][0]
    assert snes["suggestion"]["subfolder"] == "snes"
    assert sources["platforms"][2]["suggestion"] is None
    assert sources["collections"] == [{"id": 7, "name": "Favorites", "rom_count": 3}]

    r = c.post("/api/jobs", json={"target": "custom", "romm_collection_id": 7,
                                  "mirror_deletes": True, "name": "Favs"})
    job = r.get_json()
    assert r.status_code == 201
    assert job["romm_collection_id"] == 7 and job["romm_platform_id"] is None
    assert job["remote_path"] == "/" and job["mirror_deletes"] is False
