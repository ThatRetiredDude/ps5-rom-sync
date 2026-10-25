import hmac
import logging
import os

from flask import Flask, Response, jsonify, request, send_from_directory

from . import romm
from .config_store import ConfigStore
from .presets import PRESETS, RETROARCH_SYSTEMS, guess_system, remote_path_for
from .sync_engine import (LIBRARY_ROOT, SyncManager, library_is_empty, list_remote_folders,
                          resolve_source, test_connection)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _same(a, b):
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def create_app(store=None, start_manager=True, ui_password=None, ui_user=None):
    """ui_password / ui_user default to the UI_PASSWORD / UI_USER environment variables.

    With no password set the web UI is open, as before. With one set, every route except
    /healthz needs HTTP basic auth.
    """
    ui_password = os.environ.get("UI_PASSWORD", "") if ui_password is None else ui_password
    ui_user = ui_user or os.environ.get("UI_USER", "admin")
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False  # keep presets in display order
    store = store or ConfigStore()
    manager = SyncManager(store)
    if start_manager:
        manager.start()
    app.config["store"] = store
    app.config["manager"] = manager

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.before_request
    def require_login():
        if not ui_password or request.path == "/healthz":
            return None
        auth = request.authorization
        if auth and _same(auth.username or "", ui_user) and _same(auth.password or "", ui_password):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="PS5 ROM Sync"'})

    def public_ps5(settings):
        """Never send the stored FTP password back to the browser."""
        settings = dict(settings)
        settings["has_password"] = bool(settings.get("password"))
        settings["password"] = ""
        return settings

    def bad_request(message, code=400):
        return jsonify({"error": message}), code

    def job_from_body(body, existing=None):
        values = dict(existing or {})
        values.update(body)
        target = values.get("target", "custom")
        if target not in PRESETS:
            raise ValueError(f"Unknown target: {target}")
        resolve_source(values.get("source", ""))
        collection = values.get("romm_collection_id") not in (None, "")
        if collection:
            values["remote_path"] = "/"  # each game goes to its own system's folder
            values["mirror_deletes"] = False
        if not values.get("remote_path"):
            values["remote_path"] = remote_path_for(target, values.get("subfolder", ""))
        if "extensions" not in body and existing is None and not collection:
            values["extensions"] = PRESETS[target]["extensions"]
        if not values.get("name"):
            values["name"] = f"{PRESETS[target]['label']}: {values.get('source') or '/'}"
        return values

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/meta")
    def meta():
        return jsonify({"presets": PRESETS, "retroarch_systems": RETROARCH_SYSTEMS,
                        "library_root": LIBRARY_ROOT,
                        "library_empty": library_is_empty()})

    @app.get("/api/ps5")
    def get_ps5():
        return jsonify(public_ps5(store.get_ps5()))

    @app.put("/api/ps5")
    def put_ps5():
        try:
            # A missing "password" key keeps the saved one; "" clears it.
            return jsonify(public_ps5(store.set_ps5(request.get_json(force=True) or {})))
        except (TypeError, ValueError) as e:
            return bad_request(str(e))

    @app.post("/api/ps5/test")
    def test_ps5():
        settings = store.get_ps5()
        settings.update({k: v for k, v in (request.get_json(silent=True) or {}).items()
                         if k != "password" or v})
        try:
            return jsonify(test_connection(settings))
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e) or e.__class__.__name__})

    def public_romm(settings):
        """Same as public_ps5: the RomM password and token stay on the server."""
        settings = dict(settings)
        for key in ("password", "token"):
            settings[f"has_{key}"] = bool(settings.get(key))
            settings[key] = ""
        return settings

    @app.get("/api/romm")
    def get_romm():
        return jsonify(public_romm(store.get_romm()))

    @app.put("/api/romm")
    def put_romm():
        # A missing "password" / "token" key keeps the saved one; "" clears it.
        return jsonify(public_romm(store.set_romm(request.get_json(force=True) or {})))

    @app.post("/api/romm/test")
    def test_romm():
        settings = store.get_romm()
        settings.update({k: v for k, v in (request.get_json(silent=True) or {}).items()
                         if k not in ("password", "token") or v})
        try:
            return jsonify(romm.test_connection(settings))
        except romm.RommError as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.get("/api/romm/sources")
    def romm_sources():
        try:
            client = romm.RommClient(store.get_romm())
            platforms = client.platforms()
            collections = client.collections()
        except romm.RommError as e:
            return bad_request(str(e), 502)
        return jsonify({
            "platforms": [{"id": p["id"], "name": p.get("display_name") or p.get("name") or p.get("slug"),
                           "slug": p.get("slug"), "rom_count": p.get("rom_count"),
                           "suggestion": romm.suggest_target(p.get("slug"))
                           or romm.suggest_target(p.get("fs_slug"))}
                          for p in platforms],
            "collections": [{"id": c["id"], "name": c.get("name"),
                             "rom_count": c.get("rom_count", len(c.get("rom_ids") or c.get("roms") or []))}
                            for c in collections],
        })

    @app.get("/api/guess-system")
    def guess_retroarch_system():
        """Suggest the RetroArch content folder for a library folder name.

        Prefers a folder that already exists on the PS5 (asked over FTP), but never while a
        sync is running: the PS5 FTP server only handles one connection at a time.
        """
        folder = request.args.get("folder", "")
        existing, from_ps5 = [], False
        idle = not any(r["state"] in ("queued", "running") for r in manager.status().values())
        if idle and store.get_ps5().get("host"):
            try:
                existing = list_remote_folders(store.get_ps5(), PRESETS["retroarch"]["remote_base"])
                from_ps5 = True
            except Exception:  # noqa: BLE001 - the PS5 may be off; the offline guess still works
                pass
        return jsonify({"system": guess_system(folder, existing), "from_ps5": from_ps5})

    @app.get("/api/browse")
    def browse():
        rel = request.args.get("path", "")
        try:
            path = resolve_source(rel)
            dirs = sorted(d for d in os.listdir(path)
                          if not d.startswith(".") and os.path.isdir(os.path.join(path, d)))
        except (ValueError, OSError) as e:
            return bad_request(str(e))
        rel = os.path.relpath(path, os.path.realpath(LIBRARY_ROOT))
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        return jsonify({"path": rel, "dirs": dirs})

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(store.list_jobs())

    @app.post("/api/jobs")
    def add_job():
        try:
            return jsonify(store.add_job(job_from_body(request.get_json(force=True) or {}))), 201
        except (TypeError, ValueError) as e:
            return bad_request(str(e))

    @app.put("/api/jobs/<job_id>")
    def update_job(job_id):
        existing = store.get_job(job_id)
        if not existing:
            return bad_request("Job not found", 404)
        try:
            body = request.get_json(force=True) or {}
            return jsonify(store.update_job(job_id, job_from_body(body, existing)))
        except (TypeError, ValueError) as e:
            return bad_request(str(e))

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
        manager.cancel(job_id)
        if not store.delete_job(job_id):
            return bad_request("Job not found", 404)
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/run")
    def run_job(job_id):
        if not store.get_job(job_id):
            return bad_request("Job not found", 404)
        return jsonify(manager.enqueue(job_id).to_dict())

    @app.post("/api/jobs/run-all")
    def run_all():
        for job in store.list_jobs():
            if job.get("enabled", True):
                manager.enqueue(job["id"])
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        return jsonify({"ok": manager.cancel(job_id)})

    @app.get("/api/status")
    def status():
        return jsonify({"runs": manager.status(), "logs": list(manager.logs)[-200:]})

    return app


def main():
    from waitress import serve

    port = int(os.environ.get("WEB_PORT", "8585"))
    if not os.environ.get("UI_PASSWORD"):
        logging.warning("The web UI has no password: anyone on your network can use it. "
                        "Set UI_PASSWORD to require a login.")
    if library_is_empty():
        logging.warning("The library folder %s is empty or not mounted. Syncs will do nothing "
                        "until your ROMs are available.", LIBRARY_ROOT)
    logging.info("PS5 ROM Sync listening on :%d (library: %s)", port, LIBRARY_ROOT)
    serve(create_app(), host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
