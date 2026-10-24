"""Uploads a local folder to a PS5 over FTP, skipping files that already match."""

import collections
import ftplib
import logging
import os
import posixpath
import threading
import time

from . import romm
from .presets import PRESETS

log = logging.getLogger("romsync")

LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "/roms")
BLOCK_SIZE = 256 * 1024


class Cancelled(Exception):
    pass


def resolve_source(source):
    """Map a job's source to an absolute path that must stay inside LIBRARY_ROOT."""
    root = os.path.realpath(LIBRARY_ROOT)
    path = os.path.realpath(os.path.join(root, source.lstrip("/")))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError(f"Source must be inside {LIBRARY_ROOT}")
    return path


def connect(ps5, timeout=15):
    if not ps5.get("host"):
        raise ValueError("Set your PS5 IP address first")
    ftp = ftplib.FTP(timeout=timeout)
    ftp.connect(ps5["host"], int(ps5.get("port") or 2121))
    ftp.login(ps5.get("user") or "anonymous", ps5.get("password") or "")
    ftp.set_pasv(ps5.get("passive", True))
    ftp.voidcmd("TYPE I")
    return ftp


def test_connection(ps5):
    ftp = connect(ps5, timeout=8)
    try:
        welcome = ftp.getwelcome()
        cwd = ftp.pwd()
        return {"ok": True, "welcome": welcome, "cwd": cwd}
    finally:
        _quit(ftp)


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        ftp.close()


class RemoteTree:
    """Caches remote directory listings (name -> size, or None for dirs)."""

    def __init__(self, ftp):
        self.ftp = ftp
        self.cache = {}
        self.use_mlsd = True

    def listdir(self, path):
        if path in self.cache:
            return self.cache[path]
        entries = {}
        try:
            if self.use_mlsd:
                try:
                    for name, facts in self.ftp.mlsd(path, facts=["type", "size"]):
                        if name in (".", ".."):
                            continue
                        kind = facts.get("type", "file")
                        if kind in ("cdir", "pdir"):
                            continue
                        entries[name] = None if kind == "dir" else int(facts.get("size", -1))
                except ftplib.error_perm as e:
                    if str(e).startswith("550"):
                        raise
                    self.use_mlsd = False
            if not self.use_mlsd:
                entries = self._list_fallback(path)
        except ftplib.error_perm:
            entries = None  # directory does not exist
        self.cache[path] = entries
        return entries

    def _list_fallback(self, path):
        lines = []
        self.ftp.retrlines(f"LIST {path}", lines.append)
        entries = {}
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8]
            if name in (".", ".."):
                continue
            entries[name] = None if line.startswith("d") else int(parts[4])
        return entries

    def ensure_dir(self, path):
        path = path.rstrip("/")
        if not path:
            return
        parent, name = posixpath.split(path)
        parent = parent or "/"
        siblings = self.listdir(parent)
        if siblings is None:
            self.ensure_dir(parent)
            siblings = self.listdir(parent)
            if siblings is None:
                siblings = self.cache[parent] = {}
        if name in siblings and siblings[name] is None:
            return  # already exists; leave its listing to be fetched lazily
        try:
            self.ftp.mkd(path)
        except ftplib.error_perm as e:
            if not str(e).startswith("550"):
                raise
        siblings[name] = None
        self.cache[path] = {}


def walk_local(src, extensions):
    exts = {e.lower() for e in extensions}
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if exts and name.rsplit(".", 1)[-1].lower() not in exts:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            yield rel, full, os.path.getsize(full)


class JobRun:
    def __init__(self, job_id):
        self.job_id = job_id
        self.state = "queued"
        self.message = ""
        self.current_file = ""
        self.files_total = 0
        self.files_done = 0
        self.files_uploaded = 0
        self.files_skipped = 0
        self.files_deleted = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.started_at = None
        self.finished_at = None
        self.cancel_event = threading.Event()

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != "cancel_event"}


def _local_entries(full, rel, extensions):
    """(rel, full, size) for one library file, or for every file inside a folder."""
    if os.path.isdir(full):
        for sub, path, size in walk_local(full, extensions):
            yield f"{rel}/{sub}", path, size
    elif os.path.isfile(full):
        exts = {e.lower() for e in extensions}
        if not exts or full.rsplit(".", 1)[-1].lower() in exts:
            yield rel, full, os.path.getsize(full)


def plan_files(job, romm_settings=None, logger=log):
    """Work out what to copy. Returns (files, mirror_root): files are
    (label, local_path, size, remote_path); mirror_root is set when every file lives under
    one PS5 folder, which is what "delete files no longer in the library" needs."""
    remote_root = job["remote_path"].rstrip("/") or "/"
    platform_id, collection_id = job.get("romm_platform_id"), job.get("romm_collection_id")

    if platform_id is None and collection_id is None:
        src = resolve_source(job["source"])
        if not os.path.isdir(src):
            raise FileNotFoundError(f"Source folder not found: {job['source'] or '/'}")
        files = [(rel, full, size, posixpath.join(remote_root, rel))
                 for rel, full, size in walk_local(src, job["extensions"])]
        return files, remote_root

    client = romm.RommClient(romm_settings or {})
    roms = client.roms(platform_id=platform_id, collection_id=collection_id)
    files, missing = [], 0
    for rom in roms:
        if platform_id is not None:
            root, exts = remote_root, job["extensions"]
        else:
            where = romm.suggest_target(romm.rom_platform_slug(rom))
            if not where:
                logger.info("[%s] skipped %s: no PS5 target for platform '%s'", job["name"],
                            rom.get("fs_name"), romm.rom_platform_slug(rom))
                continue
            root = where["remote_path"]
            exts = job["extensions"] or PRESETS[where["target"]]["extensions"]
        rel_local = romm.rom_relpath(rom)
        try:
            full = resolve_source(rel_local)
        except ValueError:
            missing += 1
            continue
        found = list(_local_entries(full, rom["fs_name"], exts))
        if not found and not os.path.exists(full):
            missing += 1
            logger.warning("[%s] %s is in RomM but not in the library mount", job["name"], rel_local)
        files.extend((rel, path, size, posixpath.join(root, rel)) for rel, path, size in found)
    if roms and missing == len(roms):
        raise FileNotFoundError(f"None of RomM's files were found under {LIBRARY_ROOT}. "
                                "Mount RomM's library folder there.")
    return files, (remote_root if platform_id is not None else None)


def run_sync(job, ps5, run: JobRun, logger=log, romm_settings=None):
    run.state = "running"
    run.started_at = time.time()
    files, mirror_root = plan_files(job, romm_settings, logger)
    run.files_total = len(files)
    run.bytes_total = sum(f[2] for f in files)
    logger.info("[%s] %d local files (%.1f MB) -> %s", job["name"], len(files),
                run.bytes_total / 1e6, mirror_root or "PS5 (per system)")

    ftp = connect(ps5)
    try:
        tree = RemoteTree(ftp)
        if mirror_root:
            tree.ensure_dir(mirror_root)
        to_upload = []
        for rel, full, size, remote in files:
            rdir, rname = posixpath.split(remote)
            listing = tree.listdir(rdir) or {}
            if listing.get(rname) == size:
                run.files_skipped += 1
                run.files_done += 1
                run.bytes_done += size
            else:
                to_upload.append((rel, full, size, remote))

        for rel, full, size, remote in to_upload:
            if run.cancel_event.is_set():
                raise Cancelled()
            run.current_file = rel
            rdir, rname = posixpath.split(remote)
            tree.ensure_dir(rdir)
            _upload(ftp, full, remote, run)
            (tree.listdir(rdir) or {})[rname] = size
            run.files_uploaded += 1
            run.files_done += 1
            logger.info("[%s] uploaded %s", job["name"], rel)

        if job.get("mirror_deletes") and mirror_root:
            local = {posixpath.relpath(remote, mirror_root) for _, _, _, remote in files}
            _mirror_deletes(ftp, tree, mirror_root, "", local, job["extensions"], run, job, logger)
    finally:
        _quit(ftp)

    run.current_file = ""
    run.state = "done"
    run.finished_at = time.time()
    run.message = (f"{run.files_uploaded} uploaded, {run.files_skipped} already there"
                   + (f", {run.files_deleted} removed" if run.files_deleted else ""))
    logger.info("[%s] done: %s", job["name"], run.message)


def _upload(ftp, full, remote, run):
    tmp = remote + ".part"
    start_bytes = run.bytes_done

    def on_block(block):
        if run.cancel_event.is_set():
            raise Cancelled()
        run.bytes_done += len(block)

    try:
        with open(full, "rb") as f:
            ftp.storbinary(f"STOR {tmp}", f, BLOCK_SIZE, callback=on_block)
    except BaseException:
        run.bytes_done = start_bytes
        _try(ftp.delete, tmp)
        raise
    try:
        ftp.rename(tmp, remote)
    except ftplib.error_perm:
        # Some servers refuse to rename over an existing file.
        _try(ftp.delete, remote)
        ftp.rename(tmp, remote)


def _mirror_deletes(ftp, tree, root, rel_dir, local, extensions, run, job, logger):
    exts = {e.lower() for e in extensions}
    path = posixpath.join(root, rel_dir) if rel_dir else root
    for name, size in list((tree.listdir(path) or {}).items()):
        rel = posixpath.join(rel_dir, name) if rel_dir else name
        if size is None:
            _mirror_deletes(ftp, tree, root, rel, local, extensions, run, job, logger)
            continue
        if exts and name.rsplit(".", 1)[-1].lower() not in exts:
            continue
        if rel not in local:
            ftp.delete(posixpath.join(root, rel))
            run.files_deleted += 1
            logger.info("[%s] removed %s (not in library)", job["name"], rel)


def _try(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


class SyncManager:
    """Runs one job at a time (PS5 FTP servers dislike parallel uploads) and schedules jobs."""

    def __init__(self, store):
        self.store = store
        self.runs = {}
        self.queue = collections.deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_started = {}
        self.logs = collections.deque(maxlen=500)
        self.logger = logging.getLogger("romsync.jobs")
        self.logger.addHandler(_DequeHandler(self.logs))

    def start(self):
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()

    def enqueue(self, job_id):
        with self.lock:
            current = self.runs.get(job_id)
            if current and current.state in ("queued", "running"):
                return current
            run = JobRun(job_id)
            self.runs[job_id] = run
            self.queue.append(job_id)
            self.last_started[job_id] = time.time()
        self.wake.set()
        return run

    def cancel(self, job_id):
        with self.lock:
            run = self.runs.get(job_id)
            if not run:
                return False
            if run.state == "queued":
                self.queue.remove(job_id)
                run.state = "cancelled"
                run.message = "Cancelled"
            run.cancel_event.set()
            return True

    def status(self):
        with self.lock:
            return {jid: r.to_dict() for jid, r in self.runs.items()}

    def _worker(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while True:
                with self.lock:
                    if not self.queue:
                        break
                    job_id = self.queue.popleft()
                    run = self.runs[job_id]
                job = self.store.get_job(job_id)
                if not job:
                    continue
                try:
                    run_sync(job, self.store.get_ps5(), run, self.logger, self.store.get_romm())
                except Cancelled:
                    run.state, run.message = "cancelled", "Cancelled"
                    self.logger.info("[%s] cancelled", job["name"])
                except Exception as e:  # noqa: BLE001 - surface any failure in the UI
                    run.state, run.message = "error", str(e) or e.__class__.__name__
                    self.logger.error("[%s] failed: %s", job["name"], run.message)
                finally:
                    run.current_file = ""
                    run.finished_at = time.time()

    def _scheduler(self):
        while True:
            now = time.time()
            for job in self.store.list_jobs():
                minutes = job.get("interval_minutes") or 0
                if not job.get("enabled", True) or minutes <= 0:
                    continue
                last = self.last_started.get(job["id"])
                if last is None:
                    self.last_started[job["id"]] = now  # first run one interval after startup
                elif now - last >= minutes * 60:
                    self.enqueue(job["id"])
            time.sleep(30)


class _DequeHandler(logging.Handler):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        self.target.append({"t": record.created, "level": record.levelname,
                            "msg": record.getMessage()})
