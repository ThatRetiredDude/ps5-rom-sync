import os

import pytest

from app import sync_engine
from app.config_store import ConfigStore
from app.main import create_app
from app.sync_engine import JobRun, run_sync


@pytest.fixture
def library(tmp_path, monkeypatch):
    lib = tmp_path / "roms"
    (lib / "snes" / "Hacks").mkdir(parents=True)
    (lib / "snes" / "Chrono Trigger.sfc").write_bytes(b"a" * 5000)
    (lib / "snes" / "Hacks" / "Hack.smc").write_bytes(b"b" * 300)
    (lib / "snes" / "notes.txt").write_text("skip me")
    (lib / "gamecube").mkdir()
    (lib / "gamecube" / "Melee.rvz").write_bytes(b"c" * 9000)
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(lib))
    return lib


def job(**kw):
    base = {"name": "t", "source": "snes", "remote_path": "/data/homebrew/PPSA99169/content/snes",
            "extensions": ["sfc", "smc"], "mirror_deletes": False}
    base.update(kw)
    return base


def test_uploads_then_skips(ftp_server, library):
    run = JobRun("x")
    run_sync(job(), ftp_server, run)
    dest = ftp_server["root"] / "data/homebrew/PPSA99169/content/snes"
    assert (dest / "Chrono Trigger.sfc").read_bytes() == b"a" * 5000
    assert (dest / "Hacks" / "Hack.smc").stat().st_size == 300
    assert not (dest / "notes.txt").exists()
    assert not list(dest.rglob("*.part"))
    assert run.files_uploaded == 2 and run.state == "done"

    again = JobRun("x")
    run_sync(job(), ftp_server, again)
    assert again.files_uploaded == 0 and again.files_skipped == 2


def test_reuploads_changed_file(ftp_server, library):
    run_sync(job(), ftp_server, JobRun("x"))
    (library / "snes" / "Chrono Trigger.sfc").write_bytes(b"z" * 6000)
    run = JobRun("x")
    run_sync(job(), ftp_server, run)
    assert run.files_uploaded == 1
    dest = ftp_server["root"] / "data/homebrew/PPSA99169/content/snes/Chrono Trigger.sfc"
    assert dest.read_bytes() == b"z" * 6000


def test_mirror_deletes_only_matching_types(ftp_server, library):
    dest = ftp_server["root"] / "data/porpoise/games"
    dest.mkdir(parents=True)
    (dest / "Old.iso").write_bytes(b"x")
    (dest / "keep.txt").write_text("not a game")
    run = JobRun("x")
    run_sync(job(source="gamecube", remote_path="/data/porpoise/games",
                 extensions=["iso", "rvz"], mirror_deletes=True), ftp_server, run)
    assert (dest / "Melee.rvz").exists()
    assert not (dest / "Old.iso").exists()
    assert (dest / "keep.txt").exists()
    assert run.files_deleted == 1


def test_source_cannot_escape_library(library):
    with pytest.raises(ValueError):
        sync_engine.resolve_source("../../etc")


def test_api_flow(ftp_server, library, tmp_path):
    store = ConfigStore(str(tmp_path / "config" / "config.json"))
    app = create_app(store, start_manager=False)
    c = app.test_client()
    c.put("/api/ps5", json={"host": "127.0.0.1", "port": ftp_server["port"]})
    assert c.post("/api/ps5/test").get_json()["ok"]

    r = c.post("/api/jobs", json={"target": "retroarch", "source": "snes", "subfolder": "snes"})
    assert r.status_code == 201
    created = r.get_json()
    assert created["remote_path"] == "/data/homebrew/PPSA99169/content/snes"

    r = c.post("/api/jobs", json={"target": "porpoise", "source": "gamecube"})
    assert r.get_json()["extensions"] == ["ciso", "gcm", "gcz", "iso", "rvz", "wbfs", "wia"]
    assert r.get_json()["remote_path"] == "/data/porpoise/games"

    assert c.post("/api/jobs", json={"target": "custom", "source": "../x"}).status_code == 400
    assert c.get("/api/browse").get_json()["dirs"] == ["gamecube", "snes"]
    assert len(ConfigStore(store.path).list_jobs()) == 2
