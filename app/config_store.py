import json
import os
import threading
import uuid

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "ps5": {
        "host": os.environ.get("PS5_HOST", ""),
        "port": int(os.environ.get("PS5_FTP_PORT", "2121")),
        "user": "anonymous",
        "password": "",
        "passive": True,
    },
    "jobs": [],
}

JOB_FIELDS = {
    "name": "",
    "target": "custom",
    "source": "",
    "subfolder": "",
    "remote_path": "",
    "extensions": [],
    "interval_minutes": 0,
    "mirror_deletes": False,
    "enabled": True,
}

_lock = threading.RLock()


def _normalize_exts(value):
    if isinstance(value, str):
        value = value.replace(";", ",").replace(" ", ",").split(",")
    return sorted({e.strip().lstrip(".").lower() for e in value or [] if e.strip()})


class ConfigStore:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        merged["ps5"].update(data.get("ps5", {}))
        merged["jobs"] = data.get("jobs", [])
        return merged

    def save(self):
        with _lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            try:
                os.chmod(tmp, 0o600)  # it may hold the PS5 FTP password
            except OSError:
                pass
            os.replace(tmp, self.path)

    def get_ps5(self):
        with _lock:
            return dict(self.data["ps5"])

    def set_ps5(self, values):
        with _lock:
            ps5 = self.data["ps5"]
            for key in ("host", "user", "password"):
                if key in values:
                    ps5[key] = str(values[key]).strip()
            if "port" in values:
                ps5["port"] = int(values["port"] or 2121)
            if "passive" in values:
                ps5["passive"] = bool(values["passive"])
            self.save()
            return dict(ps5)

    def list_jobs(self):
        with _lock:
            return [dict(j) for j in self.data["jobs"]]

    def get_job(self, job_id):
        with _lock:
            for job in self.data["jobs"]:
                if job["id"] == job_id:
                    return dict(job)
        return None

    def _clean(self, values, base):
        job = dict(base)
        for key, default in JOB_FIELDS.items():
            if key in values:
                job[key] = values[key]
            job.setdefault(key, default)
        job["extensions"] = _normalize_exts(job["extensions"])
        job["interval_minutes"] = max(0, int(job["interval_minutes"] or 0))
        job["mirror_deletes"] = bool(job["mirror_deletes"])
        job["enabled"] = bool(job["enabled"])
        job["remote_path"] = "/" + str(job["remote_path"]).strip().strip("/")
        return job

    def add_job(self, values):
        with _lock:
            job = self._clean(values, {"id": uuid.uuid4().hex[:8]})
            self.data["jobs"].append(job)
            self.save()
            return job

    def update_job(self, job_id, values):
        with _lock:
            for i, job in enumerate(self.data["jobs"]):
                if job["id"] == job_id:
                    self.data["jobs"][i] = self._clean(values, job)
                    self.save()
                    return self.data["jobs"][i]
        return None

    def delete_job(self, job_id):
        with _lock:
            before = len(self.data["jobs"])
            self.data["jobs"] = [j for j in self.data["jobs"] if j["id"] != job_id]
            self.save()
            return len(self.data["jobs"]) != before
