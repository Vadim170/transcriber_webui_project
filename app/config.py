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
    "model": "FluidInference/parakeet-tdt-0.6b-v3-coreml",
    "quantization": "none",
    "threads": 6,
    "out_dir": "./transcripts",
    "language": "auto",
    "min_interval_s": 300,
    "max_interval_s": 600,
    "silence_cut_ms": 2000,
    "audio_queue_size": 2048,
}

# Keys removed during migration from old config format
_OLD_KEYS_TO_REMOVE = {
    "vad_aggressiveness", "preroll_ms", "silence_to_commit_ms",
    "min_speech_ms", "max_utterance_ms", "min_rms_utterance",
    "min_rms_frame_fallback", "full_audio_enabled", "full_audio_dir",
    "full_audio_retention_days",
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
        # Migrate: remove old keys that no longer exist
        migrated = False
        for old_key in _OLD_KEYS_TO_REMOVE:
            if old_key in data:
                del data[old_key]
                migrated = True
        # Ensure new defaults are present
        for key, default_val in DEFAULT_CONFIG.items():
            if key not in data:
                data[key] = default_val
                migrated = True
        cfg.update(data)
        if migrated:
            import logging
            logging.getLogger(__name__).info("Config migrated: removed old keys, added new interval defaults.")

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
