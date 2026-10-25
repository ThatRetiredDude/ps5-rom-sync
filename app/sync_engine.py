"""Uploads a local folder to a PS5 over FTP, skipping files that already match."""

import collections
import ftplib
import logging
import os
import posixpath
import threading
import time

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


# Seconds to wait before each retry after the FTP connection drops. The PS5 FTP payload
# serves one client at a time and falls over easily, so a short, patient backoff helps.
RETRY_DELAYS = (2, 5, 10)
# Errors that mean "the connection is gone or the server is busy", worth reconnecting for.
CONNECTION_ERRORS = (OSError, EOFError, ftplib.error_temp, ftplib.error_reply)


class PS5Unreachable(ConnectionError):
    pass


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


def run_sync(job, ps5, run: JobRun, logger=log):
    run.state = "running"
    run.started_at = time.time()
    src = resolve_source(job["source"])
    if not os.path.isdir(src):
        raise FileNotFoundError(f"Source folder not found: {job['source'] or '/'}")
    remote_root = job["remote_path"].rstrip("/") or "/"

    files = list(walk_local(src, job["extensions"]))
    run.files_total = len(files)
    run.bytes_total = sum(size for _, _, size in files)
    logger.info("[%s] %d local files (%.1f MB) -> %s", job["name"], len(files),
                run.bytes_total / 1e6, remote_root)

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
        retrying("preparing the folder", lambda: tree.ensure_dir(remote_root))
        to_upload = []
        for rel, full, size in files:
            remote = posixpath.join(remote_root, rel)
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

        if job.get("mirror_deletes"):
            local = {rel for rel, _, _ in files}
            _mirror_deletes(tree.ftp, tree, remote_root, "", local, job["extensions"], run, job, logger)
    finally:
        _quit(tree.ftp)

    run.current_file = ""
    run.state = "done"
    run.finished_at = time.time()
    run.message = (f"{run.files_uploaded} uploaded, {run.files_skipped} already there"
                   + (f", {run.files_deleted} removed" if run.files_deleted else ""))
    logger.info("[%s] done: %s", job["name"], run.message)


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
                    run_sync(job, self.store.get_ps5(), run, self.logger)
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
