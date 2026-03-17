from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .config import load_or_create_config, save_config
from .transcriber import TranscriberController, list_input_devices


def create_app():
    project_root = Path(__file__).resolve().parent.parent
    cfg = load_or_create_config(project_root)

    # Migrate plain password to hash on first run.
    if not str(cfg["password"]).startswith("pbkdf2:") and not str(cfg["password"]).startswith("scrypt:"):
        plain = cfg["password"]
        cfg["password"] = generate_password_hash(plain)
        save_config(cfg["config_path"], cfg)
        cfg["_generated_password"] = cfg.get("_generated_password") or plain

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = cfg["secret_key"]
    app.config["APP_CFG"] = cfg
    controller = TranscriberController()
    app.config["CONTROLLER"] = controller

    def logged_in() -> bool:
        return bool(session.get("ok"))

    @app.get("/")
    def index():
        if not logged_in():
            return redirect(url_for("login_page"))
        return render_template("index.html")

    @app.get("/login")
    def login_page():
        if logged_in():
            return redirect(url_for("index"))
        return render_template("login.html")

    @app.post("/api/login")
    def api_login():
        password = (request.json or {}).get("password", "")
        if check_password_hash(app.config["APP_CFG"]["password"], password):
            session["ok"] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Неверный пароль."}), 401

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    def guard():
        if not logged_in():
            return jsonify({"ok": False, "error": "Не авторизован."}), 401
        return None

    @app.get("/api/devices")
    def api_devices():
        deny = guard()
        if deny:
            return deny
        return jsonify({"ok": True, "devices": list_input_devices()})

    @app.get("/api/config")
    def api_config():
        deny = guard()
        if deny:
            return deny
        view = {k: v for k, v in app.config["APP_CFG"].items() if k in ("host", "port", "mic_device", "remote_device", "model", "threads", "out_dir", "config_path")}
        return jsonify({"ok": True, "config": view})

    @app.post("/api/config")
    def api_save_config():
        deny = guard()
        if deny:
            return deny
        data = request.json or {}
        cfg2 = app.config["APP_CFG"]
        for key in ("mic_device", "remote_device", "model", "threads", "out_dir"):
            if key in data:
                cfg2[key] = data[key]
        save_config(cfg2["config_path"], cfg2)
        return jsonify({"ok": True})

    @app.post("/api/start")
    def api_start():
        deny = guard()
        if deny:
            return deny
        body = request.json or {}
        cfg2 = app.config["APP_CFG"].copy()
        for key in ("mic_device", "remote_device", "model", "threads", "out_dir"):
            if key in body:
                cfg2[key] = body[key]
        ok, msg = controller.start(cfg2)
        status = 200 if ok else 400
        return jsonify({"ok": ok, "message": msg}), status

    @app.post("/api/stop")
    def api_stop():
        deny = guard()
        if deny:
            return deny
        ok, msg = controller.stop()
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/state")
    def api_state():
        deny = guard()
        if deny:
            return deny
        return jsonify({"ok": True, "state": controller.state()})

    return app
