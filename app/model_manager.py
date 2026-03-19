import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from .backends import KNOWN_MODELS, create_backend
from .config import WHISPER_CPP_MODELS


def _parse_size_hint(size_text: str) -> Optional[int]:
    if not size_text:
        return None
    text = size_text.replace("~", "").strip().upper()
    try:
        value_str, unit = text.split()
        value = float(value_str)
    except Exception:
        return None
    multipliers = {
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    if unit not in multipliers:
        return None
    return int(value * multipliers[unit])


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _hf_repo_dir(model_id: str) -> Path:
    return Path.home() / ".cache" / "huggingface" / "hub" / f"models--{model_id.replace('/', '--')}"


def _fluidaudio_model_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "FluidAudio" / "Models" / "parakeet-tdt-0.6b-v3-coreml"


def _whisper_model_path(model_name: str) -> Path:
    from pywhispercpp.constants import MODELS_DIR

    return Path(MODELS_DIR) / f"ggml-{model_name}.bin"


def _storage_info(model_id: str) -> dict:
    if model_id in WHISPER_CPP_MODELS:
        path = _whisper_model_path(model_id)
        downloaded = _dir_size(path)
        return {
            "cache_path": str(path),
            "downloaded_bytes": downloaded,
            "total_bytes": downloaded or None,
            "installed": path.exists(),
        }

    if model_id == "FluidInference/parakeet-tdt-0.6b-v3-coreml":
        path = _fluidaudio_model_dir()
        required = [
            path / "Preprocessor.mlmodelc",
            path / "Encoder.mlmodelc",
            path / "Decoder.mlmodelc",
            path / "JointDecision.mlmodelc",
            path / "parakeet_vocab.json",
        ]
        downloaded = _dir_size(path)
        return {
            "cache_path": str(path),
            "downloaded_bytes": downloaded,
            "total_bytes": _parse_size_hint(KNOWN_MODELS[model_id].get("size", "")),
            "installed": all(p.exists() for p in required),
        }

    if model_id in KNOWN_MODELS:
        path = _hf_repo_dir(model_id)
        downloaded = _dir_size(path)
        snapshots_dir = path / "snapshots"
        installed = snapshots_dir.exists() and any(snapshots_dir.iterdir())
        return {
            "cache_path": str(path),
            "downloaded_bytes": downloaded,
            "total_bytes": _parse_size_hint(KNOWN_MODELS[model_id].get("size", "")),
            "installed": installed,
        }

    path = Path(model_id).expanduser()
    return {
        "cache_path": str(path),
        "downloaded_bytes": _dir_size(path),
        "total_bytes": _dir_size(path) or None,
        "installed": path.exists(),
    }


class ModelManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.operations: dict[str, dict] = {}
        self.last_results: dict[str, dict] = {}

    def is_busy(self, model_id: str) -> bool:
        with self.lock:
            op = self.operations.get(model_id)
            return bool(op and op.get("running"))

    def begin(self, model_id: str, action: str):
        with self.lock:
            self.operations[model_id] = {
                "running": True,
                "action": action,
                "started_at": time.time(),
            }

    def finish(self, model_id: str, error: Optional[str] = None):
        with self.lock:
            self.operations.pop(model_id, None)
            self.last_results[model_id] = {
                "at": time.time(),
                "error": error or "",
            }

    def status(self, model_id: str) -> dict:
        info = _storage_info(model_id)
        with self.lock:
            op = dict(self.operations.get(model_id) or {})
            last = dict(self.last_results.get(model_id) or {})
        total_bytes = info.get("total_bytes")
        downloaded_bytes = info.get("downloaded_bytes") or 0
        if total_bytes:
            progress_pct = max(0.0, min(100.0, downloaded_bytes / total_bytes * 100.0))
        else:
            progress_pct = 100.0 if info.get("installed") else 0.0
        return {
            **info,
            "loading": bool(op.get("running")),
            "action": op.get("action", ""),
            "started_at": op.get("started_at"),
            "last_error": last.get("error", ""),
            "progress_pct": round(progress_pct, 1),
        }

    def ui_groups(self) -> list[dict]:
        whisper_models = []
        for model_id in WHISPER_CPP_MODELS:
            whisper_models.append({
                "id": model_id,
                "label": model_id,
                "backend": "whisper_cpp",
                "note": "whisper.cpp",
                "quantization": ["none"],
                **self.status(model_id),
            })

        external_models = []
        for model_id, info in KNOWN_MODELS.items():
            external_models.append({
                "id": model_id,
                "label": info["label"],
                "backend": info["backend"],
                "note": info.get("note", ""),
                "size": info.get("size", ""),
                "quantization": info.get("quantization", ["none"]),
                **self.status(model_id),
            })

        return [
            {"group": "whisper.cpp", "models": whisper_models},
            {"group": "Внешние модели", "models": external_models},
        ]

    def preload(self, model_id: str, quantization: str = "none", threads: int = 1) -> tuple[bool, str]:
        if self.is_busy(model_id):
            return False, "Модель уже загружается."

        def worker():
            self.begin(model_id, "preload")
            error = None
            try:
                backend = create_backend(model_id)
                backend.load(model_id, n_threads=threads, quantization=quantization)
            except Exception as exc:
                error = str(exc)
            finally:
                self.finish(model_id, error)

        threading.Thread(target=worker, daemon=True).start()
        return True, "Загрузка модели запущена."

    def delete(self, model_id: str) -> tuple[bool, str]:
        if self.is_busy(model_id):
            return False, "Нельзя удалить модель во время загрузки."

        info = _storage_info(model_id)
        path = Path(info["cache_path"])
        if not path.exists():
            return False, "Локальных файлов модели не найдено."

        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except Exception as exc:
            return False, f"Не удалось удалить модель: {exc}"
        return True, "Локальные файлы модели удалены."
