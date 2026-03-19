import json
import secrets
import socket
from pathlib import Path

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": None,
    "password": None,
    "secret_key": None,
    "mic_device": None,
    "remote_device": None,
    "model": "large-v3-turbo-q5_0",
    "quantization": "none",
    "threads": 6,
    "out_dir": "./transcripts",
    "language": "auto",
    "vad_aggressiveness": 1,
    "preroll_ms": 300,
    "silence_to_commit_ms": 450,
    "min_speech_ms": 80,
    "max_utterance_ms": 8000,
    "min_rms_utterance": 0.003,
    "min_rms_frame_fallback": 0.008,
    "audio_queue_size": 2048,
    "full_audio_enabled": True,
    "full_audio_dir": "./audio_archive",
    "full_audio_retention_days": 1,
}

WHISPER_CPP_MODELS = [
    'base', 'base-q5_1', 'base-q8_0', 'base.en', 'base.en-q5_1', 'base.en-q8_0',
    'large-v1', 'large-v2', 'large-v2-q5_0', 'large-v2-q8_0', 'large-v3', 'large-v3-q5_0',
    'large-v3-turbo', 'large-v3-turbo-q5_0', 'large-v3-turbo-q8_0', 'medium', 'medium-q5_0',
    'medium-q8_0', 'medium.en', 'medium.en-q5_0', 'medium.en-q8_0', 'small', 'small-q5_1',
    'small-q8_0', 'small.en', 'small.en-q5_1', 'small.en-q8_0', 'tiny', 'tiny-q5_1',
    'tiny-q8_0', 'tiny.en', 'tiny.en-q5_1', 'tiny.en-q8_0'
]

# Legacy alias kept for any external code that imports it.
FALLBACK_MODELS = WHISPER_CPP_MODELS


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def load_or_create_config(project_root: Path) -> dict:
    config_path = project_root / "config.json"
    cfg = DEFAULT_CONFIG.copy()
    generated_password = None

    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        cfg.update(data)

    if not cfg.get("port"):
        cfg["port"] = find_free_port(cfg["host"])
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_urlsafe(32)
    if not cfg.get("password"):
        generated_password = secrets.token_urlsafe(12)
        cfg["password"] = generated_password

    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg["config_path"] = str(config_path)
    cfg["project_root"] = str(project_root)
    cfg["_generated_password"] = generated_password
    return cfg


def save_config(config_path: str, data: dict):
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    Path(config_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
