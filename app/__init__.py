import json
import secrets
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, emit

from .backends import KNOWN_MODELS
from .config import WHISPER_CPP_MODELS, load_or_create_config, save_config
from .model_manager import ModelManager
from .transcriber import (
    INTERVAL_CONFIG_KEYS,
    TARGET_SAMPLE_RATE,
    IntervalWriter,
    TranscriberController,
    list_input_devices,
)
from .backends import preflight_backend

socketio = SocketIO(async_mode="threading")


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware datetime (UTC if no tzinfo)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_intervals(out_dir: Path) -> list[dict]:
    """Read all intervals from intervals.jsonl."""
    path = Path(out_dir) / "intervals.jsonl"
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "interval":
                records.append(rec)
    return records


def _build_state_payload(controller: TranscriberController) -> dict:
    state = controller.state()
    state["current_interval"] = controller.get_current_interval()
    return {"state": state}


def _build_overview_payload(controller: TranscriberController, out_dir: str) -> dict:
    all_intervals = _read_intervals(out_dir)
    overview = []
    for rec in all_intervals:
        overview.append({
            "start_at": rec.get("start_at"),
            "end_at": rec.get("end_at"),
            "duration_s": rec.get("duration_s"),
        })
    return {
        "intervals": overview,
        "current": controller.get_current_interval(),
    }


def create_app():
    project_root = Path(__file__).resolve().parent.parent
    cfg = load_or_create_config(project_root)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = cfg["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["APP_CFG"] = cfg
    socketio.init_app(app)
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")
    model_manager = ModelManager()
    controller = TranscriberController(model_manager=model_manager)
    app.config["CONTROLLER"] = controller
    app.config["MODEL_MANAGER"] = model_manager

    def logged_in() -> bool:
        return bool(session.get("ok"))

    @socketio.on("connect")
    def handle_socket_connect():
        if not logged_in():
            return False
        emit("state_update", _build_state_payload(controller))
        out_dir = controller.current_config.get("out_dir") or app.config["APP_CFG"].get("out_dir", "./transcripts")
        emit("overview_update", _build_overview_payload(controller, out_dir))

    def _socket_state_emitter():
        while True:
            time.sleep(1.0)
            out_dir = controller.current_config.get("out_dir") or app.config["APP_CFG"].get("out_dir", "./transcripts")
            socketio.emit("state_update", _build_state_payload(controller), namespace="/")
            socketio.emit("overview_update", _build_overview_payload(controller, out_dir), namespace="/")

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
    @limiter.limit("10 per minute")
    def api_login():
        password = (request.json or {}).get("password", "")
        if secrets.compare_digest(str(app.config["APP_CFG"]["password"]), password):
            session["ok"] = True
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Неверный пароль."}), 401

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    def guard() -> Response | None:
        if not logged_in():
            return jsonify({"ok": False, "error": "Не авторизован."}), 401
        return None

    @app.get("/api/devices")
    def api_devices():
        deny = guard()
        if deny:
            return deny
        return jsonify({"ok": True, "devices": list_input_devices()})

    @app.get("/api/models/status")
    def api_models_status():
        deny = guard()
        if deny:
            return deny
        groups = app.config["MODEL_MANAGER"].ui_groups()
        for group in groups:
            for model in group["models"]:
                available, error = preflight_backend(model["id"], "none")
                model["available"] = available
                model["error"] = error
        return jsonify({"ok": True, "groups": groups})

    @app.post("/api/models/preload")
    def api_models_preload():
        deny = guard()
        if deny:
            return deny
        body = request.json or {}
        model_id = (body.get("model") or "").strip()
        quantization = (body.get("quantization") or "none").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "Не указана модель."}), 400
        ok, error = preflight_backend(model_id, quantization)
        if not ok:
            return jsonify({"ok": False, "error": error}), 400
        ok, msg = app.config["MODEL_MANAGER"].preload(model_id, quantization=quantization, threads=1)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.post("/api/models/delete")
    def api_models_delete():
        deny = guard()
        if deny:
            return deny
        body = request.json or {}
        model_id = (body.get("model") or "").strip()
        if not model_id:
            return jsonify({"ok": False, "error": "Не указана модель."}), 400
        ok, msg = app.config["MODEL_MANAGER"].delete(model_id)
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    @app.get("/api/config")
    def api_config():
        deny = guard()
        if deny:
            return deny
        visible = {
            "host", "port", "mic_device", "remote_device", "model",
            "quantization", "threads", "out_dir", "config_path",
        } | set(INTERVAL_CONFIG_KEYS)
        view = {k: v for k, v in app.config["APP_CFG"].items() if k in visible}
        return jsonify({"ok": True, "config": view})

    @app.post("/api/config")
    def api_save_config():
        deny = guard()
        if deny:
            return deny
        data = request.json or {}
        cfg2 = app.config["APP_CFG"]
        writable = {
            "mic_device", "remote_device", "model", "quantization",
            "threads", "out_dir",
        } | set(INTERVAL_CONFIG_KEYS)
        for key in writable:
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
        startable = {
            "mic_device", "remote_device", "model", "quantization",
            "threads", "out_dir",
        } | set(INTERVAL_CONFIG_KEYS)
        for key in startable:
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
        return jsonify({"ok": True, **_build_state_payload(controller)})

    @app.get("/history")
    def history_page():
        if not logged_in():
            return redirect(url_for("login_page"))
        return render_template("history.html")

    # ── Intervals API ─────────────────────────────────────────

    @app.get("/api/intervals")
    def api_intervals():
        deny = guard()
        if deny:
            return deny
        from_str = request.args.get("from", "")
        to_str = request.args.get("to", "")
        if not from_str or not to_str:
            return jsonify({"ok": False, "error": "Параметры 'from' и 'to' обязательны."}), 400
        try:
            from_dt = _parse_dt(from_str)
            to_dt = _parse_dt(to_str)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"Неверный формат даты: {exc}"}), 400
        if from_dt >= to_dt:
            return jsonify({"ok": False, "error": "'from' должен быть раньше 'to'."}), 400
        out_dir = controller.current_config.get("out_dir") or app.config["APP_CFG"].get("out_dir", "./transcripts")
        all_intervals = _read_intervals(out_dir)
        # Return all intervals that overlap the range (full interval even if partial overlap)
        result = []
        for rec in all_intervals:
            try:
                iv_start = _parse_dt(rec["start_at"])
                iv_end = _parse_dt(rec["end_at"])
            except (KeyError, ValueError):
                continue
            # Overlaps if interval starts before range ends AND interval ends after range starts
            if iv_start < to_dt and iv_end > from_dt:
                result.append(rec)
        return jsonify({
            "ok": True,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "count": len(result),
            "intervals": result,
        })

    @app.get("/api/intervals/overview")
    def api_intervals_overview():
        deny = guard()
        if deny:
            return deny
        out_dir = controller.current_config.get("out_dir") or app.config["APP_CFG"].get("out_dir", "./transcripts")
        return jsonify({"ok": True, **_build_overview_payload(controller, out_dir)})

    @app.get("/api/transcriptions")
    def api_transcriptions():
        deny = guard()
        if deny:
            return deny
        from_str = request.args.get("from", "")
        to_str = request.args.get("to", "")
        if not from_str or not to_str:
            return jsonify({"ok": False, "error": "Параметры 'from' и 'to' обязательны."}), 400
        try:
            from_dt = _parse_dt(from_str)
            to_dt = _parse_dt(to_str)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"Неверный формат даты: {exc}"}), 400
        if from_dt >= to_dt:
            return jsonify({"ok": False, "error": "'from' должен быть раньше 'to'."}), 400
        
        out_dir = Path(controller.current_config.get("out_dir") or app.config["APP_CFG"].get("out_dir", "./transcripts"))
        combined_path = out_dir / "combined.jsonl"
        
        if not combined_path.exists():
            return jsonify({
                "ok": True,
                "from": from_dt.isoformat(),
                "to": to_dt.isoformat(),
                "count": 0,
                "utterances": [],
            })
        
        # Read utterances that fall completely within the time range
        utterances = []
        with combined_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                if rec.get("type") != "utterance":
                    continue
                
                try:
                    utt_start = _parse_dt(rec["start_at"])
                    utt_end = _parse_dt(rec["end_at"])
                except (KeyError, ValueError):
                    continue
                
                # Include only utterances that are completely within the range
                if utt_start >= from_dt and utt_end <= to_dt:
                    utterances.append(rec)
        
        return jsonify({
            "ok": True,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "count": len(utterances),
            "utterances": utterances,
        })
    
    @app.get("/api/voice-activity")
    def api_voice_activity():
        """Get voice activity statistics (trigger counts by hour/day)."""
        deny = guard()
        if deny:
            return deny
        
        from .voice_activity_tracker import get_tracker
        tracker = get_tracker()
        
        if not tracker:
            return jsonify({"ok": False, "error": "Трекер не инициализирован."}), 500
        
        stat_type = request.args.get("type", "hourly")  # 'hourly' or 'daily'
        from_str = request.args.get("from", "")
        to_str = request.args.get("to", "")
        
        date_from = from_str if from_str else None
        date_to = to_str if to_str else None
        
        if stat_type == "daily":
            stats = tracker.get_daily_stats(date_from, date_to)
        else:
            stats = tracker.get_hourly_stats(date_from, date_to)
        
        return jsonify({"ok": True, **stats})

    if not app.config.get("_SOCKETIO_STATE_TASK_STARTED"):
        threading.Thread(target=_socket_state_emitter, daemon=True).start()
        app.config["_SOCKETIO_STATE_TASK_STARTED"] = True

    return app
