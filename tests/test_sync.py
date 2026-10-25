import os
import socket
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from app import sync_engine
from app.config_store import ConfigStore
from app.main import create_app
from app.sync_engine import JobRun, run_sync


def _start_server(root, handler_base=FTPHandler):
    auth = DummyAuthorizer()
    auth.add_anonymous(str(root), perm="elradfmwMT")
    handler = type("H", (handler_base,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    port = server.socket.getsockname()[1]
    t = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1}, daemon=True)
    t.start()
    return server, {"host": "127.0.0.1", "port": port, "user": "anonymous", "password": "",
                    "root": root, "handler": handler}


class FtpsrvLikeHandler(FTPHandler):
    """Mimics the quirks of ftpsrv (the PS5 FTP payload) that we hit on real hardware.

    * DELE is answered with `226 File deleted` instead of `250`.
    * MLSD ignores its path argument and lists the root; only a bare MLSD lists the cwd.
      (Emulated by rewriting `MLSD <path>` to `MLSD /`.)
    * OPTS, NLST and EPSV are not implemented (502).
    """

    def respond(self, resp, *args, **kwargs):
        if resp.startswith("250 File removed"):
            resp = "226 File deleted"
        return super().respond(resp, *args, **kwargs)

    seen = None  # set per server: every raw command line received

    def pre_process_command(self, line, cmd, arg):
        if self.seen is not None:
            self.seen.append(line)
        if cmd == "MLSD" and arg:
            # ftpsrv lists the *root* when MLSD gets an argument it does not understand.
            # Emulate that by rewriting the command to MLSD of "/".
            line, arg = "MLSD /", "/"
        return super().pre_process_command(line, cmd, arg)

    def _unsupported(self, line):
        self.respond("502 Command not recognized")

    ftp_OPTS = ftp_NLST = ftp_EPSV = _unsupported


@pytest.fixture
def ftp_server(tmp_path):
    root = tmp_path / "ps5"
    root.mkdir()
    server, info = _start_server(root)
    yield info
    server.close_all()


@pytest.fixture
def ftpsrv_server(tmp_path):
    root = tmp_path / "ps5-ftpsrv"
    root.mkdir()
    server, info = _start_server(root, FtpsrvLikeHandler)
    info["handler"].seen = info["seen"] = []
    yield info
    server.close_all()


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


def test_mirror_deletes_accepts_ftpsrv_226_reply(ftpsrv_server, library):
    # ftpsrv answers DELE with "226 File deleted"; ftplib's delete() raises on that.
    dest = ftpsrv_server["root"] / "data/porpoise/games"
    dest.mkdir(parents=True)
    (dest / "Old1.iso").write_bytes(b"x")
    (dest / "Old2.iso").write_bytes(b"x")
    run = JobRun("x")
    run_sync(job(source="gamecube", remote_path="/data/porpoise/games",
                 extensions=["iso", "rvz"], mirror_deletes=True), ftpsrv_server, run)
    assert run.state == "done" and run.files_deleted == 2
    assert sorted(p.name for p in dest.iterdir()) == ["Melee.rvz"]


def test_listing_never_sends_optional_commands_to_ftpsrv(ftpsrv_server, library):
    run_sync(job(), ftpsrv_server, JobRun("x"))
    again = JobRun("x")
    run_sync(job(), ftpsrv_server, again)
    # Skipping proves the folder was listed correctly (MLSD <path> would list "/").
    assert again.files_uploaded == 0 and again.files_skipped == 2
    verbs = [line.split(" ")[0] for line in ftpsrv_server["seen"]]
    assert "NLST" not in verbs and "EPSV" not in verbs and "OPTS" not in verbs
    assert not [line for line in ftpsrv_server["seen"] if line.startswith("MLSD ")]


def test_falls_back_to_list_when_mlsd_is_refused(tmp_path, library):
    class NoMlsd(FTPHandler):
        seen = []

        def pre_process_command(self, line, cmd, arg):
            self.seen.append(line)
            return super().pre_process_command(line, cmd, arg)

        def ftp_MLSD(self, path):
            self.respond("502 Command not recognized")

    root = tmp_path / "ps5-nomlsd"
    root.mkdir()
    server, ps5 = _start_server(root, NoMlsd)
    try:
        ps5["handler"].seen = []
        run_sync(job(), ps5, JobRun("x"))
        again = JobRun("x")
        run_sync(job(), ps5, again)
        assert again.files_uploaded == 0 and again.files_skipped == 2
        verbs = [line.split(" ")[0] for line in ps5["handler"].seen]
        assert verbs.count("MLSD") == 2  # once per sync (one connection each), then LIST
        assert "LIST" in verbs
    finally:
        server.close_all()


def test_remote_tree_reports_missing_directory(ftp_server):
    ftp = sync_engine.connect(ftp_server)
    try:
        tree = sync_engine.RemoteTree(ftp)
        assert tree.listdir("/nope/not/here") is None
        assert tree.listdir("/") == {}
    finally:
        ftp.close()


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(sync_engine, "RETRY_DELAYS", (0, 0, 0))


def test_unreachable_ps5_gets_a_helpful_message(library, no_backoff):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket is closed
    ps5 = {"host": "127.0.0.1", "port": port, "user": "anonymous", "password": ""}
    with pytest.raises(sync_engine.PS5Unreachable) as exc:
        run_sync(job(), ps5, JobRun("x"))
    assert "only accepts one" in str(exc.value) and f"127.0.0.1:{port}" in str(exc.value)


def test_retries_when_server_is_busy_at_first(tmp_path, library, no_backoff):
    class BusyFirst(FTPHandler):
        calls = []

        def handle(self):
            self.calls.append(1)
            if len(self.calls) == 1:
                self.respond("421 Too many connections")
                self.close_when_done()
            else:
                super().handle()

    root = tmp_path / "ps5-busy"
    root.mkdir()
    server, ps5 = _start_server(root, BusyFirst)
    try:
        ps5["handler"].calls = []
        run = JobRun("x")
        run_sync(job(), ps5, run)
        assert run.files_uploaded == 2 and len(ps5["handler"].calls) == 2
    finally:
        server.close_all()


def test_reconnects_and_retries_when_connection_drops_mid_upload(tmp_path, library, no_backoff):
    class DropsOnce(FTPHandler):
        stors = []

        def ftp_STOR(self, file, mode="w"):
            self.stors.append(file)
            if len(self.stors) == 1:
                self.close()  # the PS5 FTP payload just hangs up
                return None
            return super().ftp_STOR(file, mode)

    root = tmp_path / "ps5-drops"
    root.mkdir()
    server, ps5 = _start_server(root, DropsOnce)
    try:
        ps5["handler"].stors = []
        run = JobRun("x")
        run_sync(job(), ps5, run)
        dest = root / "data/homebrew/PPSA99169/content/snes"
        assert run.state == "done" and run.files_uploaded == 2
        assert (dest / "Chrono Trigger.sfc").stat().st_size == 5000
        assert not list(dest.rglob("*.part"))
        assert run.bytes_done == run.bytes_total  # the failed attempt is not counted twice
    finally:
        server.close_all()


def test_gives_up_after_the_last_retry(tmp_path, library, no_backoff):
    class AlwaysDrops(FTPHandler):
        def ftp_STOR(self, file, mode="w"):
            self.close()

    root = tmp_path / "ps5-dead"
    root.mkdir()
    server, ps5 = _start_server(root, AlwaysDrops)
    try:
        with pytest.raises(Exception):
            run_sync(job(), ps5, JobRun("x"))
    finally:
        server.close_all()


def test_empty_source_folder_warns_instead_of_reporting_success(library, tmp_path, caplog):
    (library / "empty").mkdir()
    ps5 = {"host": "127.0.0.1", "port": 1}  # never contacted: there is nothing to sync
    run = JobRun("x")
    with caplog.at_level("WARNING", logger="romsync"):
        run_sync(job(source="empty"), ps5, run)
    assert run.state == "warning"
    assert "No files found in /empty" in run.message and "Nothing was uploaded or deleted" in run.message
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_empty_library_never_triggers_mirror_deletes(ftp_server, tmp_path, monkeypatch):
    # A NAS that is unplugged leaves an empty mount point. Mirroring that to the PS5
    # would delete every game, so the sync must stop before touching the PS5.
    lib = tmp_path / "unmounted"
    (lib / "gamecube").mkdir(parents=True)
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(lib))
    dest = ftp_server["root"] / "data/porpoise/games"
    dest.mkdir(parents=True)
    (dest / "Precious.iso").write_bytes(b"x" * 10)
    run = JobRun("x")
    run_sync(job(source="gamecube", remote_path="/data/porpoise/games", extensions=["iso"],
                 mirror_deletes=True), ftp_server, run)
    assert run.state == "warning" and run.files_deleted == 0
    assert (dest / "Precious.iso").exists()


def test_missing_source_on_unmounted_library_explains_why(tmp_path, monkeypatch):
    lib = tmp_path / "mountpoint"
    lib.mkdir()  # what Docker leaves behind when the NAS is not mounted
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(lib))
    with pytest.raises(FileNotFoundError, match="empty or not mounted"):
        run_sync(job(source="snes"), {"host": "127.0.0.1", "port": 1}, JobRun("x"))


def test_meta_reports_empty_library(tmp_path, monkeypatch):
    lib = tmp_path / "mountpoint"
    lib.mkdir()
    monkeypatch.setattr(sync_engine, "LIBRARY_ROOT", str(lib))
    c = create_app(ConfigStore(str(tmp_path / "c" / "config.json")), start_manager=False).test_client()
    assert c.get("/api/meta").get_json()["library_empty"] is True
    (lib / "snes").mkdir()
    assert c.get("/api/meta").get_json()["library_empty"] is False
