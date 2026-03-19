import atexit
import json
import queue
import re
import shutil
import struct
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import webrtcvad

try:
    import psutil
except Exception:
    psutil = None

from .backends import (
    ASRBackend,
    KNOWN_MODELS,
    create_backend,
    preflight_backend,
)
from .config import FALLBACK_MODELS

# ---------------------------------------------------------------------------
# Fixed protocol constants
# ---------------------------------------------------------------------------
TARGET_SAMPLE_RATE = 16000
DTYPE = "int16"
FRAME_MS = 30
MIN_TEXT_CHARS = 2
COMBINED_SPLIT_GAP_S = 5.0
COMBINED_FLUSH_IDLE_S = 5.0
WAV_HEADER_BYTES = 44
PCM_SAMPLE_WIDTH_BYTES = 2
ARCHIVE_SEGMENT_MERGE_GAP_S = 0.35
ARCHIVE_CLEANUP_INTERVAL_S = 60.0
RETRANSCRIBE_CHUNK_S = 45.0

# ---------------------------------------------------------------------------
# Tunable defaults — overridden at runtime from config.json via _audio_cfg()
# ---------------------------------------------------------------------------
AUDIO_DEFAULTS = {
    "language": "auto",
    "vad_aggressiveness": 1,
    "preroll_ms": 300,
    "silence_to_commit_ms": 450,
    "min_speech_ms": 80,
    "max_utterance_ms": 8000,
    "min_rms_utterance": 0.003,
    "min_rms_frame_fallback": 0.008,
    "audio_queue_size": 2048,
}

AUDIO_CONFIG_KEYS = list(AUDIO_DEFAULTS.keys())

FULL_AUDIO_DEFAULTS = {
    "full_audio_enabled": True,
    "full_audio_dir": "./audio_archive",
    "full_audio_retention_days": 1,
}

FULL_AUDIO_CONFIG_KEYS = list(FULL_AUDIO_DEFAULTS.keys())

GARBAGE_PATTERNS = [
    re.compile(r"^\[(?:BLANK_AUDIO|Ambient|Motor|Noise|Music|Applause|M|S|Speech|Silence)\]$", re.I),
    re.compile(r"^\[[^\]]{1,24}\]$"),
]


def _audio_cfg(config: dict) -> dict:
    """Merge user config with AUDIO_DEFAULTS, coercing value types."""
    merged = dict(AUDIO_DEFAULTS)
    for k, default_val in AUDIO_DEFAULTS.items():
        if k in config and config[k] is not None:
            try:
                if isinstance(default_val, str):
                    merged[k] = str(config[k])
                else:
                    merged[k] = type(default_val)(config[k])
            except (ValueError, TypeError):
                pass
    return merged


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def _full_audio_cfg(config: dict) -> dict:
    retention_default = FULL_AUDIO_DEFAULTS["full_audio_retention_days"]
    try:
        retention_days = int(config.get("full_audio_retention_days", retention_default))
    except (TypeError, ValueError):
        retention_days = retention_default
    return {
        "full_audio_enabled": _coerce_bool(config.get("full_audio_enabled", FULL_AUDIO_DEFAULTS["full_audio_enabled"])),
        "full_audio_dir": str(config.get("full_audio_dir") or FULL_AUDIO_DEFAULTS["full_audio_dir"]).strip() or FULL_AUDIO_DEFAULTS["full_audio_dir"],
        "full_audio_retention_days": max(1, retention_days),
    }


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def normalize_audio_source(source: str) -> Optional[str]:
    value = str(source or "").strip().lower()
    aliases = {
        "mic": "mic",
        "microphone": "mic",
        "remote": "remote",
        "system": "remote",
        "system_audio": "remote",
    }
    return aliases.get(value)


def archive_day_key(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def archive_day_start(dt: datetime) -> datetime:
    local = dt.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def archive_day_dir(root_dir: Path, day_key: str) -> Path:
    return root_dir / day_key


def archive_wav_path(root_dir: Path, day_key: str, role: str) -> Path:
    return archive_day_dir(root_dir, day_key) / f"{role}.wav"


def archive_manifest_path(root_dir: Path, day_key: str, role: str) -> Path:
    return archive_day_dir(root_dir, day_key) / f"{role}.segments.jsonl"


def iter_archive_day_keys(root_dir: Path, from_dt: datetime, to_dt: datetime) -> list[str]:
    if from_dt >= to_dt:
        return []
    cur = archive_day_start(from_dt)
    end = archive_day_start(to_dt)
    day_keys = []
    while cur <= end:
        day_keys.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return day_keys


def _write_wav_header(handle, data_bytes: int):
    handle.seek(0)
    handle.write(struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        TARGET_SAMPLE_RATE,
        TARGET_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES,
        PCM_SAMPLE_WIDTH_BYTES,
        PCM_SAMPLE_WIDTH_BYTES * 8,
        b"data",
        data_bytes,
    ))
    handle.flush()


def _open_appendable_wav(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size < WAV_HEADER_BYTES:
        with path.open("wb") as fh:
            _write_wav_header(fh, 0)
    handle = path.open("r+b")
    data_bytes = max(0, path.stat().st_size - WAV_HEADER_BYTES)
    _write_wav_header(handle, data_bytes)
    handle.seek(WAV_HEADER_BYTES + data_bytes)
    return handle, data_bytes


def read_archive_pcm(path: Path, sample_start: int, sample_count: int) -> bytes:
    if sample_count <= 0 or not path.exists():
        return b""
    byte_start = WAV_HEADER_BYTES + max(0, int(sample_start)) * PCM_SAMPLE_WIDTH_BYTES
    byte_count = max(0, int(sample_count)) * PCM_SAMPLE_WIDTH_BYTES
    with path.open("rb") as fh:
        fh.seek(byte_start)
        return fh.read(byte_count)


def iter_archive_segments(root_dir: Path, role: str, from_dt: datetime, to_dt: datetime) -> list[dict]:
    root_dir = Path(root_dir).expanduser()
    role = normalize_audio_source(role)
    if role is None or from_dt >= to_dt or not root_dir.exists():
        return []
    segments = []
    for day_key in iter_archive_day_keys(root_dir, from_dt, to_dt):
        manifest_path = archive_manifest_path(root_dir, day_key, role)
        wav_path = archive_wav_path(root_dir, day_key, role)
        if not manifest_path.exists() or not wav_path.exists():
            continue
        with manifest_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    start_at = datetime.fromisoformat(item["start_at"])
                    end_at = datetime.fromisoformat(item["end_at"])
                    sample_start = int(item.get("sample_start", 0))
                    sample_end = int(item.get("sample_end", sample_start))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if sample_end <= sample_start or end_at <= from_dt or start_at >= to_dt:
                    continue
                segments.append({
                    "role": role,
                    "day_key": day_key,
                    "path": wav_path,
                    "start_at": start_at,
                    "end_at": end_at,
                    "sample_start": sample_start,
                    "sample_end": sample_end,
                })
    return sorted(segments, key=lambda item: item["start_at"])


def interval_sample_count(start_at: datetime, end_at: datetime) -> int:
    return max(0, int(round((end_at - start_at).total_seconds() * TARGET_SAMPLE_RATE)))


def clean_transcribed_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.strip("-—– ")


def is_garbage_text(text: str) -> bool:
    text = clean_transcribed_text(text)
    if not text or len(text) < MIN_TEXT_CHARS:
        return True
    if any(p.fullmatch(text) for p in GARBAGE_PATTERNS):
        return True
    return sum(ch.isalnum() for ch in text) < 2


def count_cyrillic(text: str) -> int:
    return sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")


def count_latin(text: str) -> int:
    return sum(1 for ch in text if "a" <= ch.lower() <= "z")


def score_text_for_lang(text: str, lang: str) -> int:
    text = clean_transcribed_text(text)
    if is_garbage_text(text):
        return -10000
    cyr = count_cyrillic(text)
    lat = count_latin(text)
    alnum = sum(ch.isalnum() for ch in text)
    score = alnum * 2 + len(text)
    if lang == "ru":
        score += cyr * 4 - lat * 4
    elif lang == "en":
        score += lat * 4 - cyr * 4
    else:
        score += max(cyr, lat) * 2
    if len(text.split()) >= 2:
        score += 10
    return score


def pick_best_candidate(candidates: list[dict]) -> Optional[dict]:
    best = None
    best_score = -10_000_000
    for cand in candidates:
        lang = cand.get("language") or "auto"
        score = score_text_for_lang(cand.get("text", ""), lang)
        if lang in ("ru", "en"):
            score += 5
        if cand.get("is_auto"):
            score += 30
        if score > best_score:
            best_score = score
            best = cand
    if best is None:
        return None
    best = dict(best)
    best["text"] = clean_transcribed_text(best.get("text", ""))
    return None if is_garbage_text(best["text"]) else best


def build_archive_audio(
    root_dir: Path,
    role: str,
    from_dt: datetime,
    to_dt: datetime,
    live_segments: Optional[list[dict]] = None,
) -> tuple[np.ndarray, int]:
    role = normalize_audio_source(role)
    if role is None:
        return np.zeros(0, dtype=np.int16), 0
    segments = iter_archive_segments(root_dir, role, from_dt, to_dt)
    if live_segments:
        for seg in live_segments:
            try:
                start_at = seg["start_at"]
                end_at = seg["end_at"]
                sample_start = int(seg["sample_start"])
                sample_end = int(seg["sample_end"])
                path = Path(seg["path"])
            except (KeyError, TypeError, ValueError):
                continue
            if sample_end <= sample_start or end_at <= from_dt or start_at >= to_dt:
                continue
            segments.append({
                "role": role,
                "path": path,
                "start_at": start_at,
                "end_at": end_at,
                "sample_start": sample_start,
                "sample_end": sample_end,
            })
    segments.sort(key=lambda item: item["start_at"])
    total_samples = interval_sample_count(from_dt, to_dt)
    if total_samples <= 0:
        return np.zeros(0, dtype=np.int16), 0
    parts: list[np.ndarray] = []
    covered_samples = 0
    cursor = from_dt
    for seg in segments:
        overlap_start = max(cursor, from_dt, seg["start_at"])
        overlap_end = min(to_dt, seg["end_at"])
        if overlap_end <= overlap_start:
            continue
        if overlap_start > cursor:
            parts.append(np.zeros(interval_sample_count(cursor, overlap_start), dtype=np.int16))
        rel_start_samples = interval_sample_count(seg["start_at"], overlap_start)
        sample_start = int(seg["sample_start"]) + rel_start_samples
        sample_count = interval_sample_count(overlap_start, overlap_end)
        payload = read_archive_pcm(Path(seg["path"]), sample_start, sample_count)
        raw_samples = len(payload) // PCM_SAMPLE_WIDTH_BYTES
        chunk = np.frombuffer(payload, dtype=np.int16)
        if len(chunk) < sample_count:
            chunk = np.pad(chunk, (0, sample_count - len(chunk)), constant_values=0)
        elif len(chunk) > sample_count:
            chunk = chunk[:sample_count]
        parts.append(chunk.astype(np.int16, copy=False))
        covered_samples += min(sample_count, raw_samples)
        cursor = overlap_end
    if cursor < to_dt:
        parts.append(np.zeros(interval_sample_count(cursor, to_dt), dtype=np.int16))
    audio = np.concatenate(parts) if parts else np.zeros(total_samples, dtype=np.int16)
    if len(audio) < total_samples:
        audio = np.pad(audio, (0, total_samples - len(audio)), constant_values=0)
    elif len(audio) > total_samples:
        audio = audio[:total_samples]
    return audio.astype(np.int16, copy=False), covered_samples


def utterance_to_record(utt: "Utterance") -> dict:
    return {
        "type": "utterance",
        "role": utt.role,
        "text": utt.text,
        "start_s": round(utt.start_s, 2),
        "end_s": round(utt.end_s, 2),
        "start_at": utt.start_at,
        "end_at": utt.end_at,
        "language": utt.language,
    }


def format_combined_utterances(utterances: list["Utterance"]) -> list[dict]:
    sorted_items = sorted(utterances, key=lambda item: (item.start_at, item.role, item.start_s))
    return [
        {
            **utterance_to_record(item),
            "words": count_words(item.text),
        }
        for item in sorted_items
    ]


def list_input_devices():
    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev["max_input_channels"]) > 0:
            devices.append({
                "id": idx,
                "name": dev["name"],
                "max_input_channels": int(dev["max_input_channels"]),
                "default_samplerate": int(round(float(dev.get("default_samplerate") or 0))),
            })
    return devices


def model_is_valid(model_text: str) -> tuple[bool, str]:
    if not model_text or not str(model_text).strip():
        return False, "Поле модели пустое."
    if model_text in KNOWN_MODELS:
        return True, model_text
    p = Path(str(model_text)).expanduser()
    if p.exists() and p.is_file():
        return True, str(p)
    if model_text in FALLBACK_MODELS:
        return True, model_text
    return False, (
        "Модель должна быть известным именем (whisper.cpp / Voxtral / "
        "Canary / Parakeet / Qwen3-ASR) либо существующим путём к .bin файлу."
    )


@dataclass
class Utterance:
    role: str
    text: str
    start_s: float
    end_s: float
    start_at: str
    end_at: str
    language: Optional[str] = None


@dataclass
class SourceMetrics:
    role: str
    enabled: bool = False
    device_id: Optional[int] = None
    device_name: str = "—"
    status: str = "idle"
    queue_size: int = 0
    current_buffer_ms: float = 0.0
    current_utterance_ms: float = 0.0
    dropped_chunks: int = 0
    busy: bool = False
    last_audio_sec: float = 0.0
    last_processing_sec: float = 0.0
    last_rtf: float = 0.0
    lag_estimate_sec: float = 0.0
    last_language: str = "—"
    last_error: str = ""
    utterances: int = 0
    words: int = 0
    last_text: str = ""
    last_commit_at: str = "—"


class MetricsStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = iso_now()
        self.sources = {
            "mic": SourceMetrics(role="mic"),
            "remote": SourceMetrics(role="remote"),
        }
        self.history = deque(maxlen=8000)
        self.processing_history = deque(maxlen=8000)
        self.logs = deque(maxlen=400)
        self.last_write_at = "—"
        self.running = False
        self.loading = False
        self.model_loaded = False
        self.model_name = ""
        self.out_dir = ""
        self.server_error = ""
        self.process = psutil.Process() if psutil else None
        self.archive = {
            "enabled": False,
            "dir": "",
            "retention_days": 1,
            "files": {"mic": 0, "remote": 0},
        }

    def reset_for_run(self, model_name: str, out_dir: str, full_audio_cfg: Optional[dict] = None):
        archive_cfg = full_audio_cfg or FULL_AUDIO_DEFAULTS
        with self.lock:
            self.started_at = iso_now()
            self.history.clear()
            self.processing_history.clear()
            self.logs.clear()
            self.last_write_at = "—"
            self.server_error = ""
            self.model_name = model_name
            self.out_dir = out_dir
            self.model_loaded = False
            self.archive = {
                "enabled": bool(archive_cfg.get("full_audio_enabled")),
                "dir": str(archive_cfg.get("full_audio_dir") or ""),
                "retention_days": int(archive_cfg.get("full_audio_retention_days") or 1),
                "files": {"mic": 0, "remote": 0},
            }
            for role in self.sources:
                enabled = self.sources[role].enabled
                device_id = self.sources[role].device_id
                device_name = self.sources[role].device_name
                self.sources[role] = SourceMetrics(role=role, enabled=enabled, device_id=device_id, device_name=device_name)

    def set_running(self, running: bool, loading: bool = False):
        with self.lock:
            self.running = running
            self.loading = loading

    def set_model_loaded(self):
        with self.lock:
            self.model_loaded = True
            self.loading = False

    def set_server_error(self, message: str):
        with self.lock:
            self.server_error = message
            self.loading = False
            self.running = False

    def set_source_config(self, role: str, enabled: bool, device_id: Optional[int], device_name: str):
        with self.lock:
            src = self.sources[role]
            src.enabled = enabled
            src.device_id = device_id
            src.device_name = device_name

    def set_archive_file_size(self, role: str, size_bytes: int):
        with self.lock:
            if role in self.archive["files"]:
                self.archive["files"][role] = int(size_bytes)

    def update_source(self, role: str, **kwargs):
        with self.lock:
            src = self.sources[role]
            for k, v in kwargs.items():
                setattr(src, k, v)

    def record_processing(self, role: str, audio_sec: float, proc_sec: float, lag_estimate_sec: float):
        with self.lock:
            src = self.sources[role]
            src.last_audio_sec = round(audio_sec, 3)
            src.last_processing_sec = round(proc_sec, 3)
            src.last_rtf = round(proc_sec / audio_sec, 3) if audio_sec > 0 else 0.0
            src.lag_estimate_sec = round(lag_estimate_sec, 3)
            self.processing_history.append({
                "ts": time.time(),
                "role": role,
                "audio_sec": audio_sec,
                "proc_sec": proc_sec,
                "rtf": src.last_rtf,
                "lag": src.lag_estimate_sec,
            })

    def record_utterance(self, utt: Utterance):
        words = count_words(utt.text)
        with self.lock:
            src = self.sources[utt.role]
            src.utterances += 1
            src.words += words
            src.last_language = utt.language or "—"
            src.last_text = utt.text
            src.last_commit_at = utt.end_at
            self.last_write_at = utt.end_at
            item = {
                "ts": time.time(),
                "role": utt.role,
                "words": words,
                "language": utt.language or "unknown",
                "text": utt.text,
                "at": utt.start_at,
            }
            self.history.append(item)
            self.logs.append(item)

    def snapshot(self, out_dir: str):
        now = time.time()
        with self.lock:
            cutoff_1h = now - 3600
            by_role_last_hour = {"mic": 0, "remote": 0}
            langs = {"mic": Counter(), "remote": Counter()}
            total_words = 0
            total_utterances = 0
            for item in self.history:
                total_words += item["words"]
                total_utterances += 1
                langs[item["role"]][item["language"]] += 1
                if item["ts"] >= cutoff_1h:
                    by_role_last_hour[item["role"]] += item["words"]

            process_cpu = process_rss = thread_count = None
            system_cpu = system_mem = None
            if psutil:
                try:
                    system_cpu = psutil.cpu_percent(interval=None)
                    system_mem = psutil.virtual_memory().percent
                    process_cpu = self.process.cpu_percent(interval=None)
                    process_rss = self.process.memory_info().rss
                    thread_count = self.process.num_threads()
                except Exception:
                    pass

            files = {
                "mic": file_size(Path(out_dir) / "mic.jsonl"),
                "remote": file_size(Path(out_dir) / "remote.jsonl"),
                "combined": file_size(Path(out_dir) / "combined.jsonl"),
            }
            return {
                "session": {
                    "started_at": self.started_at,
                    "running": self.running,
                    "loading": self.loading,
                    "model_loaded": self.model_loaded,
                    "server_error": self.server_error,
                    "последняя_запись": self.last_write_at,
                    "слов_всего": total_words,
                    "фраз_всего": total_utterances,
                    "слов_за_час": by_role_last_hour,
                    "размер_логов": files,
                },
                "archive": dict(self.archive),
                "sources": {k: asdict(v) for k, v in self.sources.items()},
                "languages": {
                    role: dict(counter.most_common()) for role, counter in langs.items()
                },
                "history": list(self.history),
                "processing_history": list(self.processing_history),
                "logs": list(self.logs),
                "system": {
                    "cpu_percent": system_cpu,
                    "memory_percent": system_mem,
                    "process_cpu_percent": process_cpu,
                    "process_rss": process_rss,
                    "threads": thread_count,
                },
            }


class JsonlWriter:
    def __init__(self, out_dir: Path, metrics: MetricsStore):
        self.metrics = metrics
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.combined_path = out_dir / "combined.jsonl"
        self.role_paths = {"mic": out_dir / "mic.jsonl", "remote": out_dir / "remote.jsonl"}
        self._lock = threading.Lock()
        self._pending_combined: Optional[Utterance] = None
        self._pending_last_update_monotonic: Optional[float] = None
        self._stop_flusher = threading.Event()
        for p in [self.combined_path, *self.role_paths.values()]:
            p.touch(exist_ok=True)
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()
        atexit.register(self.close)

    def _record(self, utt: Utterance) -> dict:
        return utterance_to_record(utt)

    def _append_jsonl(self, path: Path, obj: dict):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

    def _flush_pending_combined_locked(self):
        if self._pending_combined is not None:
            self._append_jsonl(self.combined_path, self._record(self._pending_combined))
            self._pending_combined = None
            self._pending_last_update_monotonic = None

    def _flush_loop(self):
        while not self._stop_flusher.is_set():
            time.sleep(1.0)
            with self._lock:
                if self._pending_combined is None or self._pending_last_update_monotonic is None:
                    continue
                if time.monotonic() - self._pending_last_update_monotonic >= COMBINED_FLUSH_IDLE_S:
                    self._flush_pending_combined_locked()

    def write(self, utt: Utterance):
        now_mono = time.monotonic()
        with self._lock:
            rec = self._record(utt)
            self._append_jsonl(self.role_paths[utt.role], rec)
            self.metrics.record_utterance(utt)
            if self._pending_combined is None:
                self._pending_combined = Utterance(**vars(utt))
                self._pending_last_update_monotonic = now_mono
                return
            gap_s = max(0.0, utt.start_s - self._pending_combined.end_s)
            same_role = self._pending_combined.role == utt.role
            if same_role and gap_s < COMBINED_SPLIT_GAP_S:
                self._pending_combined.text = (self._pending_combined.text + " " + utt.text).strip()
                self._pending_combined.end_s = utt.end_s
                self._pending_combined.end_at = utt.end_at
                if utt.language:
                    self._pending_combined.language = utt.language
                self._pending_last_update_monotonic = now_mono
                return
            self._flush_pending_combined_locked()
            self._pending_combined = Utterance(**vars(utt))
            self._pending_last_update_monotonic = now_mono

    def close(self):
        self._stop_flusher.set()
        if hasattr(self, "_flusher") and self._flusher.is_alive():
            self._flusher.join(timeout=1.5)
        with self._lock:
            self._flush_pending_combined_locked()


@dataclass
class ArchiveSourceState:
    role: str
    day_key: Optional[str] = None
    wav_path: Optional[Path] = None
    manifest_path: Optional[Path] = None
    handle: Optional[object] = None
    data_bytes: int = 0
    sample_count: int = 0
    open_segment: Optional[dict] = None


class FullAudioWriter:
    def __init__(self, root_dir: Path, metrics: MetricsStore, cfg: dict):
        self.root_dir = Path(root_dir).expanduser()
        self.metrics = metrics
        self.enabled = bool(cfg.get("full_audio_enabled"))
        self.retention_days = max(1, int(cfg.get("full_audio_retention_days") or 1))
        self._lock = threading.Lock()
        self._states = {role: ArchiveSourceState(role=role) for role in ("mic", "remote")}
        self._last_cleanup_monotonic = 0.0
        if self.enabled:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        atexit.register(self.close)

    def _touch_cleanup(self, now_dt: datetime):
        if not self.enabled:
            return
        now_mono = time.monotonic()
        if now_mono - self._last_cleanup_monotonic < ARCHIVE_CLEANUP_INTERVAL_S:
            return
        self.cleanup(now_dt)
        self._last_cleanup_monotonic = now_mono

    def cleanup(self, now_dt: Optional[datetime] = None):
        if not self.enabled or not self.root_dir.exists():
            return
        now_dt = (now_dt or datetime.now().astimezone()).astimezone()
        cutoff = now_dt - timedelta(days=self.retention_days)
        for day_dir in self.root_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                day_start = datetime.fromisoformat(day_dir.name).replace(tzinfo=cutoff.tzinfo)
            except ValueError:
                continue
            if day_start + timedelta(days=1) <= cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)

    def _flush_segment_locked(self, state: ArchiveSourceState):
        segment = state.open_segment
        if not segment or state.manifest_path is None:
            return
        payload = {
            "type": "segment",
            "role": state.role,
            "start_at": segment["start_at"].isoformat(timespec="milliseconds"),
            "end_at": segment["end_at"].isoformat(timespec="milliseconds"),
            "sample_start": int(segment["sample_start"]),
            "sample_end": int(segment["sample_end"]),
        }
        with state.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
        state.open_segment = None

    def _close_state_locked(self, state: ArchiveSourceState):
        self._flush_segment_locked(state)
        if state.handle is not None:
            try:
                _write_wav_header(state.handle, state.data_bytes)
            finally:
                state.handle.close()
        state.day_key = None
        state.wav_path = None
        state.manifest_path = None
        state.handle = None
        state.data_bytes = 0
        state.sample_count = 0

    def _ensure_day_locked(self, role: str, day_key: str):
        state = self._states[role]
        if state.day_key == day_key and state.handle is not None:
            return state
        if state.handle is not None:
            self._close_state_locked(state)
        wav_path = archive_wav_path(self.root_dir, day_key, role)
        manifest_path = archive_manifest_path(self.root_dir, day_key, role)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.touch(exist_ok=True)
        handle, data_bytes = _open_appendable_wav(wav_path)
        state.day_key = day_key
        state.wav_path = wav_path
        state.manifest_path = manifest_path
        state.handle = handle
        state.data_bytes = data_bytes
        state.sample_count = data_bytes // PCM_SAMPLE_WIDTH_BYTES
        self.metrics.set_archive_file_size(role, file_size(wav_path))
        return state

    def _append_part_locked(self, role: str, audio: np.ndarray, start_at: datetime, end_at: datetime):
        if len(audio) == 0:
            return
        state = self._ensure_day_locked(role, archive_day_key(start_at))
        payload = audio.astype(np.int16, copy=False).tobytes()
        state.handle.seek(WAV_HEADER_BYTES + state.data_bytes)
        state.handle.write(payload)
        state.data_bytes += len(payload)
        _write_wav_header(state.handle, state.data_bytes)
        sample_start = state.sample_count
        sample_end = sample_start + len(audio)
        state.sample_count = sample_end
        segment = state.open_segment
        if segment is None:
            state.open_segment = {
                "start_at": start_at,
                "end_at": end_at,
                "sample_start": sample_start,
                "sample_end": sample_end,
            }
        else:
            gap_s = (start_at - segment["end_at"]).total_seconds()
            if gap_s <= ARCHIVE_SEGMENT_MERGE_GAP_S:
                segment["end_at"] = max(segment["end_at"], end_at)
                segment["sample_end"] = sample_end
            else:
                self._flush_segment_locked(state)
                state.open_segment = {
                    "start_at": start_at,
                    "end_at": end_at,
                    "sample_start": sample_start,
                    "sample_end": sample_end,
                }
        if state.wav_path is not None:
            self.metrics.set_archive_file_size(role, file_size(state.wav_path))

    def append(self, role: str, audio: np.ndarray, start_at: datetime, end_at: datetime):
        if not self.enabled or len(audio) == 0:
            return
        role = normalize_audio_source(role)
        if role is None:
            return
        start_at = start_at.astimezone()
        end_at = end_at.astimezone()
        if end_at <= start_at:
            end_at = start_at + timedelta(seconds=len(audio) / TARGET_SAMPLE_RATE)
        with self._lock:
            self._touch_cleanup(end_at)
            cursor_dt = start_at
            cursor_idx = 0
            total_samples = len(audio)
            while cursor_idx < total_samples:
                next_day = archive_day_start(cursor_dt) + timedelta(days=1)
                seconds_until_boundary = max(0.0, (next_day - cursor_dt).total_seconds())
                max_samples = max(1, int(round(seconds_until_boundary * TARGET_SAMPLE_RATE))) if seconds_until_boundary > 0 else total_samples - cursor_idx
                remaining = total_samples - cursor_idx
                part_samples = remaining if remaining <= max_samples else max_samples
                part = audio[cursor_idx:cursor_idx + part_samples]
                part_end = cursor_dt + timedelta(seconds=len(part) / TARGET_SAMPLE_RATE)
                self._append_part_locked(role, part, cursor_dt, part_end)
                cursor_dt = part_end
                cursor_idx += part_samples

    def snapshot_active_segments(self, from_dt: Optional[datetime] = None, to_dt: Optional[datetime] = None) -> dict[str, list[dict]]:
        snapshots = {"mic": [], "remote": []}
        with self._lock:
            for role, state in self._states.items():
                segment = state.open_segment
                if segment is None or state.wav_path is None:
                    continue
                start_at = segment["start_at"]
                end_at = segment["end_at"]
                if from_dt is not None and end_at <= from_dt:
                    continue
                if to_dt is not None and start_at >= to_dt:
                    continue
                snapshots[role].append({
                    "role": role,
                    "path": state.wav_path,
                    "day_key": state.day_key,
                    "start_at": start_at,
                    "end_at": end_at,
                    "sample_start": int(segment["sample_start"]),
                    "sample_end": int(segment["sample_end"]),
                    "is_live": True,
                })
        return snapshots

    def close(self):
        with self._lock:
            for state in self._states.values():
                if state.handle is not None:
                    self._close_state_locked(state)


class AudioStreamWorker(threading.Thread):
    def __init__(self, role: str, device: Optional[int], backend: ASRBackend,
                 writer: JsonlWriter, audio_writer: Optional[FullAudioWriter], metrics: MetricsStore,
                 stop_event: threading.Event, session_started_at: datetime,
                 backend_lock: Optional[threading.Lock],
                 acfg: dict):
        super().__init__(daemon=True)
        self.role = role
        self.device = device
        self.backend = backend
        self.writer = writer
        self.audio_writer = audio_writer
        self.metrics = metrics
        self.stop_event = stop_event
        self.session_started_at = session_started_at
        self.backend_lock = backend_lock
        self.acfg = acfg
        self.vad = webrtcvad.Vad(acfg["vad_aggressiveness"])
        self.q: queue.Queue[tuple[datetime, np.ndarray]] = queue.Queue(maxsize=acfg["audio_queue_size"])
        self.stream: Optional[sd.InputStream] = None
        self.start_monotonic = time.monotonic()
        self.current_frames = []
        self.current_start_s: Optional[float] = None
        self.last_speech_ts: Optional[float] = None
        self.enabled = device is not None
        self.input_sr = TARGET_SAMPLE_RATE
        self.input_channels = 1
        self.frame_samples = TARGET_SAMPLE_RATE * FRAME_MS // 1000
        self.preroll_frames: deque[np.ndarray] = deque(maxlen=max(1, acfg["preroll_ms"] // FRAME_MS))
        self.lag_estimate_sec = 0.0
        self._partial_buf = np.zeros(0, dtype=np.int16)
        self._tx_queue: queue.Queue = queue.Queue(maxsize=256)
        self._tx_thread: Optional[threading.Thread] = None
        self.metrics.update_source(self.role, status="ready")

    def callback(self, indata, frames, time_info, status):
        try:
            self.q.put_nowait((datetime.now().astimezone(), np.array(indata, copy=True)))
        except queue.Full:
            with self.metrics.lock:
                self.metrics.sources[self.role].dropped_chunks += 1

    def open_stream(self):
        if self.device is None:
            return
        info = sd.query_devices(self.device)
        max_in = int(info["max_input_channels"])
        if max_in < 1:
            raise RuntimeError(f"device {self.device} has no input channels")
        self.input_channels = max_in
        self.input_sr = int(round(float(info.get("default_samplerate") or 48000))) or 48000
        input_frame_samples = max(1, int(round(self.input_sr * FRAME_MS / 1000.0)))
        self.frame_samples = TARGET_SAMPLE_RATE * FRAME_MS // 1000
        self.stream = sd.InputStream(
            samplerate=self.input_sr,
            blocksize=input_frame_samples,
            channels=self.input_channels,
            dtype=DTYPE,
            device=self.device,
            callback=self.callback,
            latency="low",
        )
        self.stream.start()
        self.metrics.update_source(self.role, status="listening")

    def current_duration_ms(self) -> float:
        return 0.0 if not self.current_frames else sum(len(x) for x in self.current_frames) / TARGET_SAMPLE_RATE * 1000.0

    def reset_current(self):
        self.current_frames = []
        self.current_start_s = None
        self.last_speech_ts = None
        self.metrics.update_source(self.role, current_buffer_ms=0.0, current_utterance_ms=0.0)

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text.strip("-—– ")

    def _is_garbage_text(self, text: str) -> bool:
        if not text or len(text) < MIN_TEXT_CHARS:
            return True
        if any(p.fullmatch(text) for p in GARBAGE_PATTERNS):
            return True
        return sum(ch.isalnum() for ch in text) < 2

    def _iso_at(self, seconds_from_start: float) -> str:
        dt = self.session_started_at + timedelta(seconds=max(0.0, seconds_from_start))
        return dt.astimezone().isoformat(timespec="seconds")

    def _to_mono_16k_i16(self, audio: np.ndarray) -> np.ndarray:
        mono = audio[:, 0] if audio.ndim == 2 and audio.shape[1] == 1 else (audio.mean(axis=1) if audio.ndim == 2 else audio)
        mono = np.asarray(mono, dtype=np.float32)
        if self.input_sr != TARGET_SAMPLE_RATE:
            src_len = len(mono)
            if src_len == 0:
                return np.zeros(0, dtype=np.int16)
            dst_len = max(1, int(round(src_len * TARGET_SAMPLE_RATE / self.input_sr)))
            x_old = np.linspace(0.0, 1.0, num=src_len, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
            mono = np.interp(x_new, x_old, mono)
        return np.clip(np.round(mono), -32768, 32767).astype(np.int16)

    def _frame_rms(self, mono16: np.ndarray) -> float:
        if len(mono16) == 0:
            return 0.0
        pcm = mono16.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(np.square(pcm))))

    def _count_cyrillic(self, text: str) -> int:
        return sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")

    def _count_latin(self, text: str) -> int:
        return sum(1 for ch in text if "a" <= ch.lower() <= "z")

    def _score_text_for_lang(self, text: str, lang: str) -> int:
        text = self._clean_text(text)
        if self._is_garbage_text(text):
            return -10000
        cyr = self._count_cyrillic(text)
        lat = self._count_latin(text)
        alnum = sum(ch.isalnum() for ch in text)
        score = alnum * 2 + len(text)
        if lang == "ru":
            score += cyr * 4 - lat * 4
        elif lang == "en":
            score += lat * 4 - cyr * 4
        else:
            score += max(cyr, lat) * 2
        if len(text.split()) >= 2:
            score += 10
        return score

    def _transcribe_once(self, media, forced_language: Optional[str]):
        if self.backend_lock is not None:
            with self.backend_lock:
                result = self.backend.transcribe_once(media, forced_language)
        else:
            result = self.backend.transcribe_once(media, forced_language)
        result["text"] = self._clean_text(result.get("text", ""))
        return result

    def _pick_best_candidate(self, candidates):
        best = None
        best_score = -10_000_000
        for cand in candidates:
            lang = cand["language"] or "auto"
            score = self._score_text_for_lang(cand["text"], lang)
            if lang in ("ru", "en"):
                score += 5
            if cand.get("is_auto"):
                score += 30
            if score > best_score:
                best_score = score
                best = cand
        return None if best is None or self._is_garbage_text(best["text"]) else best

    # ------------------------------------------------------------------
    # Async transcription: dedicated thread so audio capture never stalls
    # ------------------------------------------------------------------

    def _transcription_loop(self):
        """Drain _tx_queue in a dedicated thread."""
        while True:
            try:
                item = self._tx_queue.get(timeout=0.5)
            except queue.Empty:
                if self.stop_event.is_set() and self._tx_queue.empty():
                    break
                continue
            if item is None:
                break
            audio, start_s, end_s = item
            self._do_transcribe(audio, start_s, end_s)

    def _do_transcribe(self, audio: np.ndarray, start_s: float, end_s: float):
        audio_sec = len(audio) / TARGET_SAMPLE_RATE
        audio_f32 = audio.astype(np.float32) / 32768.0
        t0 = time.monotonic()
        self.metrics.update_source(self.role, busy=True, status="processing")
        lang = self.acfg["language"]
        try:
            if lang and lang != "auto":
                candidates = [self._transcribe_once(audio_f32, lang)]
            elif self.backend.supports_multi_candidate:
                auto_res = self._transcribe_once(audio_f32, None)
                auto_res["is_auto"] = True
                candidates = [
                    auto_res,
                    self._transcribe_once(audio_f32, "ru"),
                    self._transcribe_once(audio_f32, "en"),
                ]
            else:
                auto_res = self._transcribe_once(audio_f32, None)
                auto_res["is_auto"] = True
                candidates = [auto_res]
        except Exception as e:
            self.metrics.update_source(self.role, busy=False, status="listening", last_error=str(e))
            return
        proc_sec = time.monotonic() - t0
        self.lag_estimate_sec = max(0.0, self.lag_estimate_sec + proc_sec - audio_sec)
        self.metrics.record_processing(self.role, audio_sec, proc_sec, self.lag_estimate_sec)
        best = self._pick_best_candidate(candidates)
        self.metrics.update_source(self.role, busy=False, status="listening")
        if not best:
            return
        seg_end = start_s + best["seg_end_rel"] if best["seg_end_rel"] is not None else end_s
        utt = Utterance(role=self.role, text=best["text"], start_s=start_s, end_s=seg_end,
                        start_at=self._iso_at(start_s), end_at=self._iso_at(seg_end),
                        language=best["language"])
        self.writer.write(utt)
        self.metrics.update_source(self.role, last_language=utt.language or "—",
                                   last_text=utt.text, last_commit_at=utt.end_at)

    def commit_current(self, end_s: float):
        if not self.current_frames or self.current_start_s is None:
            self.reset_current(); return
        audio = np.concatenate(self.current_frames)
        if len(audio) == 0:
            self.reset_current(); return
        duration_ms = len(audio) / TARGET_SAMPLE_RATE * 1000.0
        if duration_ms < self.acfg["min_speech_ms"]:
            self.reset_current(); return
        if self._frame_rms(audio) < self.acfg["min_rms_utterance"]:
            self.reset_current(); return
        try:
            self._tx_queue.put_nowait((audio, self.current_start_s, end_s))
        except queue.Full:
            self._do_transcribe(audio, self.current_start_s, end_s)
        self.reset_current()

    # ------------------------------------------------------------------
    # Per-frame VAD processing (extracted for partial-frame accumulation)
    # ------------------------------------------------------------------

    def _process_vad_frame(self, mono16: np.ndarray):
        self.preroll_frames.append(mono16.copy())
        now_s = time.monotonic() - self.start_monotonic
        rms = self._frame_rms(mono16)
        try:
            is_speech = self.vad.is_speech(mono16.tobytes(), TARGET_SAMPLE_RATE)
        except Exception:
            is_speech = False
        if not is_speech and rms >= self.acfg["min_rms_frame_fallback"]:
            is_speech = True
        if is_speech:
            if self.current_start_s is None:
                preroll = list(self.preroll_frames)[:-1]
                if preroll:
                    self.current_frames.extend(preroll)
                preroll_sec = (len(preroll) * self.frame_samples) / TARGET_SAMPLE_RATE
                self.current_start_s = max(0.0, now_s - (len(mono16) / TARGET_SAMPLE_RATE) - preroll_sec)
            self.current_frames.append(mono16)
            self.last_speech_ts = now_s
            if self.current_duration_ms() >= self.acfg["max_utterance_ms"]:
                self.commit_current(now_s)
        else:
            if self.current_frames:
                self.current_frames.append(mono16)
                if self.last_speech_ts is not None and (now_s - self.last_speech_ts) * 1000.0 >= self.acfg["silence_to_commit_ms"]:
                    self.commit_current(now_s)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        if not self.enabled:
            return
        try:
            self.open_stream()
        except Exception as e:
            self.metrics.update_source(self.role, status="error", last_error=str(e))
            return
        self._tx_thread = threading.Thread(target=self._transcription_loop, daemon=True)
        self._tx_thread.start()
        try:
            while not self.stop_event.is_set():
                self.metrics.update_source(self.role, queue_size=self.q.qsize(),
                    current_utterance_ms=round(self.current_duration_ms(), 1),
                    current_buffer_ms=round(self.current_duration_ms(), 1))
                try:
                    captured_at, chunk = self.q.get(timeout=0.2)
                except queue.Empty:
                    if self.current_frames and self.last_speech_ts is not None:
                        now_s = time.monotonic() - self.start_monotonic
                        if (now_s - self.last_speech_ts) * 1000.0 >= self.acfg["silence_to_commit_ms"]:
                            self.commit_current(now_s)
                    continue
                mono16 = self._to_mono_16k_i16(chunk)
                if self.audio_writer and len(mono16) > 0:
                    chunk_duration = timedelta(seconds=len(mono16) / TARGET_SAMPLE_RATE)
                    chunk_end_at = captured_at.astimezone()
                    chunk_start_at = chunk_end_at - chunk_duration
                    self.audio_writer.append(self.role, mono16, chunk_start_at, chunk_end_at)
                if len(self._partial_buf) > 0:
                    mono16 = np.concatenate([self._partial_buf, mono16])
                    self._partial_buf = np.zeros(0, dtype=np.int16)
                while len(mono16) >= self.frame_samples:
                    frame = mono16[:self.frame_samples]
                    mono16 = mono16[self.frame_samples:]
                    self._process_vad_frame(frame)
                if len(mono16) > 0:
                    self._partial_buf = mono16.copy()
        finally:
            if self.current_frames:
                self.commit_current(time.monotonic() - self.start_monotonic)
            self._tx_queue.put(None)
            if self._tx_thread:
                self._tx_thread.join(timeout=120)
            if self.stream is not None:
                self.stream.stop(); self.stream.close()
            self.metrics.update_source(self.role, status="stopped", busy=False)


class TranscriberController:
    def __init__(self, model_manager=None):
        self.metrics = MetricsStore()
        self.stop_event = None
        self.backend = None
        self.backend_lock = threading.Lock()
        self.writer = None
        self.audio_writer = None
        self.workers = []
        self.controller_lock = threading.Lock()
        self.runner_thread = None
        self.current_config = {}
        self.model_manager = model_manager

    def start(self, config: dict):
        with self.controller_lock:
            if self.runner_thread and self.runner_thread.is_alive():
                return False, "Транскрибация уже запущена."
            ok, model_value = model_is_valid(config["model"])
            if not ok:
                return False, model_value
            if self.model_manager and self.model_manager.is_busy(model_value):
                return False, "Модель уже загружается."
            ok, preflight_message = preflight_backend(
                model_value,
                quantization=str(config.get("quantization") or "none"),
            )
            if not ok:
                return False, preflight_message
            self.current_config = config.copy()
            self.current_config["model"] = model_value
            self.runner_thread = threading.Thread(target=self._run, daemon=True)
            self.runner_thread.start()
            return True, "Запуск..."

    def _run(self):
        cfg = self.current_config
        full_audio_cfg = _full_audio_cfg(cfg)
        self.metrics.reset_for_run(cfg["model"], str(Path(cfg["out_dir"]).expanduser()), full_audio_cfg=full_audio_cfg)
        self.metrics.set_running(True, loading=True)
        if self.model_manager:
            self.model_manager.begin(cfg["model"], "start")
        try:
            backend = create_backend(cfg["model"])
            backend.load(
                cfg["model"],
                n_threads=int(cfg["threads"]),
                quantization=str(cfg.get("quantization") or "none"),
            )
            self.backend = backend
            self.metrics.set_model_loaded()
        except Exception as e:
            if self.model_manager:
                self.model_manager.finish(cfg["model"], str(e))
            self.metrics.set_server_error(f"Ошибка загрузки модели: {e}")
            return
        if self.model_manager:
            self.model_manager.finish(cfg["model"], None)
        self.stop_event = threading.Event()
        session_started_at = datetime.now().astimezone()
        out_dir = Path(cfg["out_dir"]).expanduser()
        self.writer = JsonlWriter(out_dir, self.metrics)
        self.audio_writer = FullAudioWriter(Path(full_audio_cfg["full_audio_dir"]).expanduser(), self.metrics, full_audio_cfg)
        devices = {d["id"]: d["name"] for d in list_input_devices()}
        self.metrics.set_source_config("mic", cfg.get("mic_device") is not None, cfg.get("mic_device"), devices.get(cfg.get("mic_device"), "—"))
        self.metrics.set_source_config("remote", cfg.get("remote_device") is not None, cfg.get("remote_device"), devices.get(cfg.get("remote_device"), "—"))
        acfg = _audio_cfg(cfg)
        self.workers = [
            AudioStreamWorker("mic", cfg.get("mic_device"), self.backend, self.writer, self.audio_writer, self.metrics, self.stop_event, session_started_at, self.backend_lock, acfg),
            AudioStreamWorker("remote", cfg.get("remote_device"), self.backend, self.writer, self.audio_writer, self.metrics, self.stop_event, session_started_at, self.backend_lock, acfg),
        ]
        for w in self.workers:
            if w.enabled:
                w.start()
        try:
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            for w in self.workers:
                if w.enabled:
                    w.join(timeout=180)
            if self.audio_writer:
                self.audio_writer.close()
            if self.writer:
                self.writer.close()
            self.metrics.set_running(False, loading=False)

    def stop(self):
        with self.controller_lock:
            if self.stop_event:
                self.stop_event.set()
            return True, "Остановка запрошена."

    def active_archive_segments(self, from_dt: Optional[datetime] = None, to_dt: Optional[datetime] = None) -> dict[str, list[dict]]:
        if not self.audio_writer:
            return {"mic": [], "remote": []}
        return self.audio_writer.snapshot_active_segments(from_dt=from_dt, to_dt=to_dt)

    def state(self):
        out_dir = self.current_config.get("out_dir", "./transcripts")
        return self.metrics.snapshot(str(Path(out_dir).expanduser()))


class RetranscribeJobManager:
    def __init__(self, controller: Optional[TranscriberController] = None):
        self.controller = controller
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def _set_job(self, job_id: str, **fields):
        with self.lock:
            job = self.jobs.setdefault(job_id, {})
            job.update(fields)

    def _snapshot_job(self, job_id: str) -> Optional[dict]:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def start(self, config: dict, from_dt: datetime, to_dt: datetime) -> tuple[bool, dict | str]:
        if from_dt >= to_dt:
            return False, "Параметр `from` должен быть раньше `to`."
        role_coverages = {}
        archive_root = Path(config.get("full_audio_dir") or "./audio_archive").expanduser()
        live_segments = self.controller.active_archive_segments(from_dt=from_dt, to_dt=to_dt) if self.controller else {"mic": [], "remote": []}
        covered_any = False
        for role in ("mic", "remote"):
            _, covered_samples = build_archive_audio(archive_root, role, from_dt, to_dt, live_segments=live_segments.get(role))
            role_coverages[role] = covered_samples
            covered_any = covered_any or covered_samples > 0
        if not covered_any:
            return False, "За выбранный диапазон нет полного аудио для повторной транскрибации."
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "queued",
            "phase": "queued",
            "progress_pct": 0.0,
            "processed_seconds": 0.0,
            "total_seconds": 0.0,
            "current_source": "",
            "started_at": iso_now(),
            "finished_at": None,
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
            "error": "",
            "result": None,
            "role_coverages": role_coverages,
        }
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job_id, dict(config), from_dt, to_dt, live_segments), daemon=True)
        thread.start()
        return True, dict(job)

    def status(self, job_id: str) -> Optional[dict]:
        return self._snapshot_job(job_id)

    def _transcribe_interval_audio(
        self,
        backend: ASRBackend,
        backend_lock: Optional[threading.Lock],
        role: str,
        audio_i16: np.ndarray,
        from_dt: datetime,
        cfg: dict,
        processed_before: float,
        total_seconds: float,
        job_id: str,
    ) -> tuple[list[Utterance], float]:
        utterances: list[Utterance] = []
        chunk_samples = max(1, int(RETRANSCRIBE_CHUNK_S * TARGET_SAMPLE_RATE))
        processed_seconds = processed_before
        language = str(cfg.get("language") or "auto")
        for chunk_start in range(0, len(audio_i16), chunk_samples):
            chunk = audio_i16[chunk_start:chunk_start + chunk_samples]
            chunk_seconds = len(chunk) / TARGET_SAMPLE_RATE
            self._set_job(
                job_id,
                status="running",
                phase=f"transcribing_{role}",
                current_source=role,
                processed_seconds=round(processed_seconds, 2),
                progress_pct=round((processed_seconds / total_seconds) * 100.0, 1) if total_seconds > 0 else 0.0,
            )
            if len(chunk) == 0:
                continue
            if not np.any(chunk):
                processed_seconds += chunk_seconds
                continue
            audio_f32 = chunk.astype(np.float32) / 32768.0
            try:
                def call_backend(lang):
                    if backend_lock is not None:
                        with backend_lock:
                            return backend.transcribe_once(audio_f32, lang)
                    return backend.transcribe_once(audio_f32, lang)
                if language and language != "auto":
                    candidates = [call_backend(language)]
                elif backend.supports_multi_candidate:
                    auto_res = call_backend(None)
                    auto_res["is_auto"] = True
                    candidates = [auto_res, call_backend("ru"), call_backend("en")]
                else:
                    auto_res = call_backend(None)
                    auto_res["is_auto"] = True
                    candidates = [auto_res]
            except Exception as exc:
                raise RuntimeError(f"Ошибка повторной транскрибации {role}: {exc}") from exc
            best = pick_best_candidate(candidates)
            if best:
                start_s = chunk_start / TARGET_SAMPLE_RATE
                seg_end_rel = best.get("seg_end_rel")
                end_s = start_s + (float(seg_end_rel) if seg_end_rel is not None else chunk_seconds)
                start_at = (from_dt + timedelta(seconds=start_s)).astimezone().isoformat(timespec="seconds")
                end_at = (from_dt + timedelta(seconds=end_s)).astimezone().isoformat(timespec="seconds")
                utterances.append(Utterance(
                    role=role,
                    text=best["text"],
                    start_s=start_s,
                    end_s=end_s,
                    start_at=start_at,
                    end_at=end_at,
                    language=best.get("language"),
                ))
            processed_seconds += chunk_seconds
        return utterances, processed_seconds

    def _run_job(self, job_id: str, config: dict, from_dt: datetime, to_dt: datetime, live_segments: dict[str, list[dict]]):
        try:
            archive_root = Path(config.get("full_audio_dir") or "./audio_archive").expanduser()
            source_audio: dict[str, tuple[np.ndarray, int]] = {}
            total_seconds = 0.0
            self._set_job(job_id, status="running", phase="building_audio", current_source="", progress_pct=0.0)
            for role in ("mic", "remote"):
                audio_i16, covered_samples = build_archive_audio(archive_root, role, from_dt, to_dt, live_segments=live_segments.get(role))
                source_audio[role] = (audio_i16, covered_samples)
                if covered_samples > 0:
                    total_seconds += len(audio_i16) / TARGET_SAMPLE_RATE
            if total_seconds <= 0:
                raise RuntimeError("За выбранный диапазон нет полного аудио для повторной транскрибации.")
            self._set_job(job_id, total_seconds=round(total_seconds, 2), processed_seconds=0.0, phase="loading_model")
            backend_lock: Optional[threading.Lock] = None
            reuse_live_backend = (
                self.controller is not None
                and self.controller.backend is not None
                and str(self.controller.current_config.get("model")) == str(config.get("model"))
                and str(self.controller.current_config.get("quantization") or "none") == str(config.get("quantization") or "none")
            )
            if reuse_live_backend:
                backend = self.controller.backend
                backend_lock = self.controller.backend_lock
            else:
                backend = create_backend(config["model"])
                backend.load(
                    config["model"],
                    n_threads=int(config.get("threads") or 1),
                    quantization=str(config.get("quantization") or "none"),
                )
                backend_lock = threading.Lock()
            processed_seconds = 0.0
            utterances: list[Utterance] = []
            for role in ("mic", "remote"):
                audio_i16, covered_samples = source_audio[role]
                if covered_samples <= 0:
                    continue
                role_utterances, processed_seconds = self._transcribe_interval_audio(
                    backend, backend_lock, role, audio_i16, from_dt, config, processed_seconds, total_seconds, job_id
                )
                utterances.extend(role_utterances)
            self._set_job(job_id, phase="merging", current_source="", processed_seconds=round(total_seconds, 2), progress_pct=99.0)
            result_utterances = format_combined_utterances(utterances)
            self._set_job(
                job_id,
                status="done",
                phase="done",
                progress_pct=100.0,
                processed_seconds=round(total_seconds, 2),
                current_source="",
                finished_at=iso_now(),
                result={
                    "ok": True,
                    "from": from_dt.isoformat(),
                    "to": to_dt.isoformat(),
                    "count": len(result_utterances),
                    "utterances": result_utterances,
                    "retranscribed": True,
                },
            )
        except Exception as exc:
            self._set_job(
                job_id,
                status="error",
                phase="error",
                finished_at=iso_now(),
                error=str(exc),
            )
