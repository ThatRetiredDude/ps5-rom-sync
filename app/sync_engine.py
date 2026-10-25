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


def library_is_empty():
    """True when the library mount is missing or has nothing in it (e.g. NAS unplugged)."""
    try:
        return not any(True for _ in os.scandir(LIBRARY_ROOT))
    except OSError:
        return True


# Seconds to wait before each retry after the FTP connection drops. The PS5 FTP payload
# serves one client at a time and falls over easily, so a short, patient backoff helps.
RETRY_DELAYS = (2, 5, 10)
# Errors that mean "the connection is gone or the server is busy", worth reconnecting for.
CONNECTION_ERRORS = (OSError, EOFError, ftplib.error_temp, ftplib.error_reply)


class PS5Unreachable(ConnectionError):
    pass


# When the PS5 can't be reached (payload not running, or another FTP client has the one
# connection), a sync waits and tries again this often, for up to PS5_BUSY_RETRY_MINUTES.
RETRY_LATER_SECONDS = 60
BUSY_RETRY_MINUTES = int(os.environ.get("PS5_BUSY_RETRY_MINUTES", "30"))


def connect(ps5, timeout=15):
    if not ps5.get("host"):
        raise ValueError("Set your PS5 IP address first")
    host, port = ps5["host"], int(ps5.get("port") or 2121)
    ftp = ftplib.FTP(timeout=timeout)
    try:
        ftp.connect(host, port)
        ftp.login(ps5.get("user") or "anonymous", ps5.get("password") or "")
        ftp.set_pasv(ps5.get("passive", True))
        ftp.voidcmd("TYPE I")
    except ftplib.error_perm:
        ftp.close()
        raise  # wrong user/password: retrying will not help
    except CONNECTION_ERRORS as e:
        ftp.close()
        raise PS5Unreachable(
            f"Could not reach the PS5 FTP server at {host}:{port} ({str(e) or e.__class__.__name__}). "
            "Make sure the FTP payload is running (it stops in rest mode) and that no other "
            "FTP client is connected: the PS5 only accepts one at a time.") from e
    return ftp


def _sleep(seconds, run=None):
    """Sleep, but wake up early when the run is cancelled."""
    if run is not None:
        if run.cancel_event.wait(seconds):
            raise Cancelled()
    else:
        time.sleep(seconds)


def connect_with_retry(ps5, run=None, logger=log):
    for attempt, delay in enumerate(RETRY_DELAYS + (None,)):
        try:
            return connect(ps5)
        except PS5Unreachable as e:
            if delay is None:
                raise
            logger.warning("%s Retrying in %ss (%d/%d)...", e, delay, attempt + 1, len(RETRY_DELAYS))
            _sleep(delay, run)


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
    """Caches remote directory listings (name -> size, or None for dirs).

    PS5 FTP payloads are picky, so listings avoid every optional feature:

    * No NLST and no `OPTS MLST` (ftpsrv answers 502 to both).
    * MLSD is only ever sent *without* an argument, after a CWD. ftpsrv advertises
      MLSD in FEAT but ignores its path argument and lists `/` instead, which would
      make every folder look like it holds the PS5 root.
    * If MLSD is refused (any 5xx) we log it once and use `LIST` from then on.
    """

    def __init__(self, ftp, logger=log):
        self.ftp = ftp
        self.cache = {}
        self.use_mlsd = True
        self.logger = logger

    def listdir(self, path):
        if path in self.cache:
            return self.cache[path]
        try:
            self.ftp.cwd(path)
        except ftplib.error_perm:
            entries = None  # directory does not exist
        else:
            entries = self._read_cwd(path)
        self.cache[path] = entries
        return entries

    def _read_cwd(self, path):
        if self.use_mlsd:
            try:
                return self._list_mlsd()
            except (ftplib.error_perm, ftplib.error_reply) as e:
                self.use_mlsd = False
                self.logger.info("FTP server refused MLSD (%s); using LIST instead", e)
        return self._list_fallback(path)

    def _list_mlsd(self):
        entries = {}
        for name, facts in self.ftp.mlsd():
            if name in (".", ".."):
                continue
            kind = facts.get("type", "file")
            if kind in ("cdir", "pdir"):
                continue
            entries[name] = None if kind == "dir" else int(facts.get("size", -1))
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
            try:
                entries[name] = None if line.startswith("d") else int(parts[4])
            except ValueError:
                continue
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


def remote_rel(rel, folder_per_game=False):
    """Where a library file goes, relative to the sync's PS5 folder.

    With folder_per_game, `Hacks/Mario Kart.rvz` becomes `Hacks/Mario Kart/Mario Kart.rvz`
    (Porpoise setups usually keep one folder per game). Files already inside a folder
    named after themselves are left alone, so a second sync does not nest again.
    """
    if not folder_per_game:
        return rel
    folder, name = posixpath.split(rel)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if posixpath.basename(folder) == stem:
        return rel
    return posixpath.join(folder, stem, name)


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
        self.busy_since = None  # first time this sync found the PS5 busy or unreachable
        self.retry_at = None
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
    per_game = bool(job.get("folder_per_game"))

    if platform_id is None and collection_id is None:
        src = resolve_source(job["source"])
        if not os.path.isdir(src):
            hint = (f" The library folder {LIBRARY_ROOT} is empty or not mounted: is the drive or NAS plugged in?"
                    if library_is_empty() else "")
            raise FileNotFoundError(f"Source folder not found: {job['source'] or '/'}.{hint}")
        files = [(rel, full, size, posixpath.join(remote_root, remote_rel(rel, per_game)))
                 for rel, full, size in walk_local(src, job["extensions"])]
        return files, remote_root

    client = romm.RommClient(romm_settings or {})
    roms = client.roms(platform_id=platform_id, collection_id=collection_id)
    files, missing = [], 0
    for rom in roms:
        if platform_id is not None:
            root, exts, nest = remote_root, job["extensions"], per_game
        else:
            where = romm.suggest_target(romm.rom_platform_slug(rom))
            if not where:
                logger.info("[%s] skipped %s: no PS5 target for platform '%s'", job["name"],
                            rom.get("fs_name"), romm.rom_platform_slug(rom))
                continue
            root = where["remote_path"]
            exts = job["extensions"] or PRESETS[where["target"]]["extensions"]
            nest = per_game and where["target"] == "porpoise"
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
        files.extend((rel, path, size, posixpath.join(root, remote_rel(rel, nest)))
                     for rel, path, size in found)
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
    if not files:
        # Nothing to do, and certainly nothing to delete: an empty folder usually means the
        # drive or NAS holding the library is not mounted, not that the library was emptied.
        where = f"/{job['source']}" if job["source"] else "the library"
        why = (f"The library folder {LIBRARY_ROOT} is empty or not mounted: is the drive or NAS plugged in?"
               if library_is_empty() else
               "Check the folder, and the file types filter" + (f" ({', '.join(job['extensions'])})"
                                                              if job["extensions"] else "") + ".")
        run.state = "warning"
        run.message = f"No files found in {where}. Nothing was uploaded or deleted. {why}"
        run.finished_at = time.time()
        logger.warning("[%s] %s", job["name"], run.message)
        return

    tree = RemoteTree(connect_with_retry(ps5, run, logger), logger)

    def retrying(what, op):
        """Run op(); if the connection drops, reconnect with backoff and run it again."""
        for attempt, delay in enumerate(RETRY_DELAYS + (None,)):
            try:
                return op()
            except CONNECTION_ERRORS as e:
                if delay is None:
                    raise
                logger.warning("[%s] connection lost while %s (%s); reconnecting in %ss (%d/%d)",
                               job["name"], what, str(e) or e.__class__.__name__, delay,
                               attempt + 1, len(RETRY_DELAYS))
                _sleep(delay, run)
                _quit(tree.ftp)
                tree.ftp = connect_with_retry(ps5, run, logger)

    try:
        if mirror_root:
            retrying("preparing the folder", lambda: tree.ensure_dir(mirror_root))
        to_upload = []
        for rel, full, size, remote in files:
            rdir, rname = posixpath.split(remote)
            listing = retrying("listing the PS5 folder", lambda: tree.listdir(rdir)) or {}
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

            def put():
                tree.ensure_dir(rdir)
                _upload(tree.ftp, full, remote, run)

            retrying(f"uploading {rel}", put)
            (tree.listdir(rdir) or {})[rname] = size
            run.files_uploaded += 1
            run.files_done += 1
            logger.info("[%s] uploaded %s (%s)", job["name"], rel, _fmt_bytes(size))

        if job.get("mirror_deletes") and mirror_root:
            local = {posixpath.relpath(remote, mirror_root) for _, _, _, remote in files}
            _mirror_deletes(tree.ftp, tree, mirror_root, "", local, job["extensions"], run, job, logger)
    finally:
        _quit(tree.ftp)

    run.current_file = ""
    run.state = "done"
    run.finished_at = time.time()
    run.message = (f"{run.files_uploaded} uploaded, {run.files_skipped} already there"
                   + (f", {run.files_deleted} removed" if run.files_deleted else ""))
    logger.info("[%s] done: %s", job["name"], run.message)


def list_remote_folders(ps5, path, timeout=6):
    """Names of the sub-folders of `path` on the PS5 (empty if it does not exist)."""
    ftp = connect(ps5, timeout=timeout)
    try:
        entries = RemoteTree(ftp).listdir(path) or {}
    finally:
        _quit(ftp)
    return sorted(name for name, size in entries.items() if size is None)


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


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
        _try(_delete, ftp, tmp)
        raise
    try:
        ftp.rename(tmp, remote)
    except ftplib.error_perm:
        # Some servers refuse to rename over an existing file.
        _try(_delete, ftp, remote)
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
            _delete(ftp, posixpath.join(root, rel))
            run.files_deleted += 1
            logger.info("[%s] removed %s (not in library)", job["name"], rel)


def _delete(ftp, path):
    """DELE that accepts any 2xx reply.

    ftplib's FTP.delete() only accepts 250 (and 200). ftpsrv, the FTP payload most
    PS5 setups use, answers `226 File deleted`, which makes ftplib raise error_reply
    even though the file is gone.
    """
    ftp.voidcmd(f"DELE {path}")


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

    def enqueue(self, job_id, busy_since=None):
        with self.lock:
            current = self.runs.get(job_id)
            if current and current.state in ("queued", "running"):
                return current
            run = JobRun(job_id)
            run.busy_since = busy_since
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
            if run.state in ("queued", "waiting"):
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
                self._run_one(job_id, run)

    def _run_one(self, job_id, run):
        job = self.store.get_job(job_id)
        if not job:
            return
        try:
            run_sync(job, self.store.get_ps5(), run, self.logger, self.store.get_romm())
        except Cancelled:
            run.state, run.message = "cancelled", "Cancelled"
            self.logger.info("[%s] cancelled", job["name"])
        except PS5Unreachable as e:
            self._retry_later(job, run, e)
        except Exception as e:  # noqa: BLE001 - surface any failure in the UI
            run.state, run.message = "error", str(e) or e.__class__.__name__
            self.logger.error("[%s] failed: %s", job["name"], run.message)
        finally:
            run.current_file = ""
            run.finished_at = time.time()

    def _retry_later(self, job, run, error):
        now = time.time()
        run.busy_since = run.busy_since or run.started_at or now
        give_up_at = run.busy_since + BUSY_RETRY_MINUTES * 60
        if now + RETRY_LATER_SECONDS > give_up_at:
            run.state = "error"
            run.message = f"{error} Gave up after trying for {BUSY_RETRY_MINUTES} minutes."
            self.logger.error("[%s] failed: %s", job["name"], run.message)
            return
        run.state, run.retry_at = "waiting", now + RETRY_LATER_SECONDS
        run.message = ("The PS5 is busy or not reachable. Trying again every minute until "
                       + time.strftime("%H:%M", time.localtime(give_up_at)) + ".")
        self.logger.warning("[%s] PS5 busy or unreachable; will try again in %ds",
                            job["name"], RETRY_LATER_SECONDS)

    def _retry_waiting(self, now):
        with self.lock:
            due = [(jid, r.busy_since) for jid, r in self.runs.items()
                   if r.state == "waiting" and r.retry_at <= now]
        for job_id, busy_since in due:
            self.enqueue(job_id, busy_since)

    def _scheduler(self):
        while True:
            now = time.time()
            self._retry_waiting(now)
            for job in self.store.list_jobs():
                minutes = job.get("interval_minutes") or 0
                if not job.get("enabled", True) or minutes <= 0:
                    continue
                last = self.last_started.get(job["id"])
                if last is None:
                    self.last_started[job["id"]] = now  # first run one interval after startup
                elif now - last >= minutes * 60:
                    self.enqueue(job["id"])
            time.sleep(15)


class _DequeHandler(logging.Handler):
    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        self.target.append({"t": record.created, "level": record.levelname,
                            "msg": record.getMessage()})
