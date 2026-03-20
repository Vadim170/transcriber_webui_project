import json
import io
import secrets
import wave
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from .backends import KNOWN_MODELS
from .config import WHISPER_CPP_MODELS, load_or_create_config, save_config
from .model_manager import ModelManager
from .transcriber import (
    AUDIO_CONFIG_KEYS,
    FULL_AUDIO_CONFIG_KEYS,
    PCM_SAMPLE_WIDTH_BYTES,
    TARGET_SAMPLE_RATE,
    RetranscribeJobManager,
    TranscriberController,
    build_archive_audio,
    interval_sample_count,
    iter_archive_segments,
    list_input_devices,
    normalize_audio_source,
)
from .backends import preflight_backend


def _read_combined(out_dir: Path) -> Iterator[dict]:
    """Yield parsed utterance dicts from combined.jsonl."""
    path = Path(out_dir) / "combined.jsonl"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "utterance":
                yield rec


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware datetime (UTC if no tzinfo)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _audio_archive_dir(cfg: dict) -> Path:
    return Path(cfg.get("full_audio_dir") or "./audio_archive").expanduser()


def _audio_overview_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=24), now


def _merge_audio_coverage(segments: list[dict]) -> list[dict]:
    merged = []
    for seg in sorted(segments, key=lambda item: item["start_at"]):
        start_at = seg["start_at"]
        end_at = seg["end_at"]
        if not merged:
            merged.append({"start_at": start_at, "end_at": end_at})
            continue
        prev = merged[-1]
        if start_at <= prev["end_at"] + timedelta(milliseconds=400):
            prev["end_at"] = max(prev["end_at"], end_at)
        else:
            merged.append({"start_at": start_at, "end_at": end_at})
    return [
        {
            "start_at": item["start_at"].isoformat(),
            "end_at": item["end_at"].isoformat(),
        }
        for item in merged
    ]


def _archive_segments_for_window(cfg: dict, controller: TranscriberController, role: str, from_dt: datetime, to_dt: datetime) -> list[dict]:
    archive_dir = _audio_archive_dir(cfg)
    segments = iter_archive_segments(archive_dir, role, from_dt, to_dt)
    live_segments = controller.active_archive_segments(from_dt=from_dt, to_dt=to_dt).get(role, [])
    segments.extend(live_segments)
    return sorted(segments, key=lambda item: item["start_at"])


def _build_audio_clip(cfg: dict, controller: TranscriberController, source: str, from_dt: datetime, to_dt: datetime) -> tuple[bytes | None, str | None]:
    role = normalize_audio_source(source)
    if role is None:
        return None, "Источник должен быть `mic` или `remote`."
    if not cfg.get("full_audio_enabled", True):
        return None, "Хранение полного аудио выключено в настройках."
    if from_dt >= to_dt:
        return None, "Параметр `from` должен быть раньше `to`."
    archive_dir = _audio_archive_dir(cfg)
    live_segments = controller.active_archive_segments(from_dt=from_dt, to_dt=to_dt).get(role, [])
    audio_i16, covered_samples = build_archive_audio(archive_dir, role, from_dt, to_dt, live_segments=live_segments)
    total_samples = interval_sample_count(from_dt, to_dt)
    if total_samples <= 0:
        return None, "Пустой диапазон аудио."
    if covered_samples <= 0:
        return None, "Запись за выбранный диапазон не найдена или уже очищена."
    pcm = audio_i16.astype("int16", copy=False).tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(TARGET_SAMPLE_RATE)
        wav_file.writeframes(pcm)
    return buffer.getvalue(), None


def create_app():
    project_root = Path(__file__).resolve().parent.parent
    cfg = load_or_create_config(project_root)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = cfg["secret_key"]
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["APP_CFG"] = cfg
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")
    model_manager = ModelManager()
    controller = TranscriberController(model_manager=model_manager)
    retranscribe_manager = RetranscribeJobManager(controller=controller)
    app.config["CONTROLLER"] = controller
    app.config["MODEL_MANAGER"] = model_manager
    app.config["RETRANSCRIBE_MANAGER"] = retranscribe_manager

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
        visible = {"host", "port", "mic_device", "remote_device", "model", "quantization", "threads", "out_dir", "config_path"} | set(AUDIO_CONFIG_KEYS) | set(FULL_AUDIO_CONFIG_KEYS)
        view = {k: v for k, v in app.config["APP_CFG"].items() if k in visible}
        return jsonify({"ok": True, "config": view})

    @app.post("/api/config")
    def api_save_config():
        deny = guard()
        if deny:
            return deny
        data = request.json or {}
        cfg2 = app.config["APP_CFG"]
        writable = {"mic_device", "remote_device", "model", "quantization", "threads", "out_dir"} | set(AUDIO_CONFIG_KEYS) | set(FULL_AUDIO_CONFIG_KEYS)
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
        startable = {"mic_device", "remote_device", "model", "quantization", "threads", "out_dir"} | set(AUDIO_CONFIG_KEYS) | set(FULL_AUDIO_CONFIG_KEYS)
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
        return jsonify({"ok": True, "state": controller.state()})

    @app.get("/history")
    def history_page():
        if not logged_in():
            return redirect(url_for("login_page"))
        return render_template("history.html")

    @app.get("/api/combined/overview")
    def api_combined_overview():
        deny = guard()
        if deny:
            return deny
        out_dir = app.config["APP_CFG"].get("out_dir", "./transcripts")
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=24)
        bucket_secs = 120  # 2-minute buckets
        buckets: dict[int, dict] = {}
        for rec in _read_combined(out_dir):
            try:
                dt = _parse_dt(rec["start_at"])
            except (KeyError, ValueError):
                continue
            if dt < window_start:
                continue
            elapsed = (dt - window_start).total_seconds()
            idx = int(elapsed // bucket_secs)
            if idx not in buckets:
                bucket_ts = window_start + timedelta(seconds=idx * bucket_secs)
                buckets[idx] = {"ts": bucket_ts.isoformat(), "mic": 0, "remote": 0, "words": 0}
            role = rec.get("role", "mic")
            words = len((rec.get("text") or "").split())
            if role in buckets[idx]:
                buckets[idx][role] += 1
            buckets[idx]["words"] += words
        result = [buckets[k] for k in sorted(buckets)]
        return jsonify({"ok": True, "buckets": result, "window_hours": 24, "bucket_secs": bucket_secs})

    @app.get("/api/combined")
    def api_combined():
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
        out_dir = app.config["APP_CFG"].get("out_dir", "./transcripts")
        utterances = []
        for rec in _read_combined(out_dir):
            try:
                dt = _parse_dt(rec["start_at"])
            except (KeyError, ValueError):
                continue
            if from_dt <= dt <= to_dt:
                words = len((rec.get("text") or "").split())
                utterances.append({
                    "role": rec.get("role"),
                    "text": rec.get("text", ""),
                    "start_s": rec.get("start_s"),
                    "end_s": rec.get("end_s"),
                    "start_at": rec.get("start_at"),
                    "end_at": rec.get("end_at"),
                    "language": rec.get("language"),
                    "words": words,
                })
        return jsonify({
            "ok": True,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "count": len(utterances),
            "utterances": utterances,
        })

    @app.get("/api/audio/overview")
    def api_audio_overview():
        deny = guard()
        if deny:
            return deny
        from_str = request.args.get("from", "")
        to_str = request.args.get("to", "")
        if from_str and to_str:
            try:
                from_dt = _parse_dt(from_str)
                to_dt = _parse_dt(to_str)
            except ValueError as exc:
                return jsonify({"ok": False, "error": f"Неверный формат даты: {exc}"}), 400
        else:
            from_dt, to_dt = _audio_overview_window()
        if from_dt >= to_dt:
            return jsonify({"ok": False, "error": "'from' должен быть раньше 'to'."}), 400
        cfg2 = app.config["APP_CFG"]
        sources = {
            role: _merge_audio_coverage(_archive_segments_for_window(cfg2, controller, role, from_dt, to_dt))
            for role in ("mic", "remote")
        }
        return jsonify({
            "ok": True,
            "enabled": bool(cfg2.get("full_audio_enabled", True)),
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "sources": sources,
        })

    @app.get("/api/audio/clip")
    def api_audio_clip():
        deny = guard()
        if deny:
            return deny
        source = request.args.get("source", "")
        from_str = request.args.get("from", "")
        to_str = request.args.get("to", "")
        if not source or not from_str or not to_str:
            return jsonify({"ok": False, "error": "Параметры 'source', 'from' и 'to' обязательны."}), 400
        try:
            from_dt = _parse_dt(from_str)
            to_dt = _parse_dt(to_str)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"Неверный формат даты: {exc}"}), 400
        role = normalize_audio_source(source)
        if role is None:
            return jsonify({"ok": False, "error": "Источник должен быть `mic` или `remote`."}), 400
        if from_dt >= to_dt:
            return jsonify({"ok": False, "error": "'from' должен быть раньше 'to'."}), 400
        clip_bytes, error = _build_audio_clip(app.config["APP_CFG"], controller, source, from_dt, to_dt)
        if error:
            return jsonify({"ok": False, "error": error}), 404
        filename = f"{role}_{from_dt.strftime('%Y-%m-%dT%H-%M-%S')}_{to_dt.strftime('%Y-%m-%dT%H-%M-%S')}.wav"
        return send_file(
            io.BytesIO(clip_bytes),
            mimetype="audio/wav",
            as_attachment=True,
            download_name=filename,
        )

    @app.post("/api/audio/retranscribe")
    def api_audio_retranscribe():
        deny = guard()
        if deny:
            return deny
        body = request.json or {}
        from_str = body.get("from", "")
        to_str = body.get("to", "")
        if not from_str or not to_str:
            return jsonify({"ok": False, "error": "Параметры 'from' и 'to' обязательны."}), 400
        try:
            from_dt = _parse_dt(from_str)
            to_dt = _parse_dt(to_str)
        except ValueError as exc:
            return jsonify({"ok": False, "error": f"Неверный формат даты: {exc}"}), 400
        # Prefer the actually running/transcribing model config over saved settings:
        # the user can start a session with unsaved form values, and retranscribe
        # should use the same active model/backend instead of reloading a different one.
        live_cfg = dict(controller.current_config or {})
        cfg2 = live_cfg if live_cfg.get("model") else app.config["APP_CFG"].copy()
        ok, preflight_message = preflight_backend(
            cfg2["model"],
            quantization=str(cfg2.get("quantization") or "none"),
        )
        if not ok:
            return jsonify({"ok": False, "error": preflight_message}), 400
        started, payload = app.config["RETRANSCRIBE_MANAGER"].start(cfg2, from_dt, to_dt)
        if not started:
            return jsonify({"ok": False, "error": str(payload)}), 400
        return jsonify({"ok": True, "job": payload}), 202

    @app.get("/api/audio/retranscribe/<job_id>")
    def api_audio_retranscribe_status(job_id: str):
        deny = guard()
        if deny:
            return deny
        job = app.config["RETRANSCRIBE_MANAGER"].status(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job не найден."}), 404
        return jsonify({"ok": True, "job": job})

    return app
