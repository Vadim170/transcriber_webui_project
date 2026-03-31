import atexit
import json
import queue
import re
import threading
import time
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
from .voice_activity_tracker import init_tracker, log_voice_trigger

# ---------------------------------------------------------------------------
# Fixed protocol constants
# ---------------------------------------------------------------------------
TARGET_SAMPLE_RATE = 16000
DTYPE = "int16"
FRAME_MS = 30
MIN_TEXT_CHARS = 2
RMS_SPEECH_THRESHOLD = 0.008

# Internal chunk size for background transcription of long intervals
_TRANSCRIBE_CHUNK_S = 30.0

# ---------------------------------------------------------------------------
# Config defaults for interval engine
# ---------------------------------------------------------------------------
INTERVAL_DEFAULTS = {
    "language": "auto",
    "min_interval_s": 300,
    "max_interval_s": 600,
    "silence_cut_ms": 2000,
    "audio_queue_size": 2048,
}

INTERVAL_CONFIG_KEYS = list(INTERVAL_DEFAULTS.keys())

GARBAGE_PATTERNS = [
    re.compile(r"^\[(?:BLANK_AUDIO|Ambient|Motor|Noise|Music|Applause|M|S|Speech|Silence)\]$", re.I),
    re.compile(r"^\[[^\]]{1,24}\]$"),
]


def _interval_cfg(config: dict) -> dict:
    merged = dict(INTERVAL_DEFAULTS)
    for k, default_val in INTERVAL_DEFAULTS.items():
        if k in config and config[k] is not None:
            try:
                if isinstance(default_val, str):
                    merged[k] = str(config[k])
                else:
                    merged[k] = type(default_val)(config[k])
            except (ValueError, TypeError):
                pass
    return merged


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
    return sum(1 for ch in text if "\u0430" <= ch.lower() <= "\u044f" or ch.lower() == "\u0451")


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
        return False, "\u041f\u043e\u043b\u0435 \u043c\u043e\u0434\u0435\u043b\u0438 \u043f\u0443\u0441\u0442\u043e\u0435."
    if model_text in KNOWN_MODELS:
        return True, model_text
    p = Path(str(model_text)).expanduser()
    if p.exists() and p.is_file():
        return True, str(p)
    if model_text in FALLBACK_MODELS:
        return True, model_text
    return False, (
        "\u041c\u043e\u0434\u0435\u043b\u044c \u0434\u043e\u043b\u0436\u043d\u0430 \u0431\u044b\u0442\u044c \u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u043c \u0438\u043c\u0435\u043d\u0435\u043c (whisper.cpp / Voxtral / "
        "Canary / Parakeet / Qwen3-ASR) \u043b\u0438\u0431\u043e \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u043c \u043f\u0443\u0442\u0451\u043c \u043a .bin \u0444\u0430\u0439\u043b\u0443."
    )


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SourceMetrics:
    role: str
    enabled: bool = False
    device_id: Optional[int] = None
    device_name: str = "\u2014"
    status: str = "idle"
    queue_size: int = 0
    dropped_chunks: int = 0
    busy: bool = False
    last_audio_sec: float = 0.0
    last_processing_sec: float = 0.0
    last_rtf: float = 0.0
    lag_estimate_sec: float = 0.0
    last_language: str = "\u2014"
    last_error: str = ""
    words: int = 0
    last_text: str = ""
    last_commit_at: str = "\u2014"


class MetricsStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = iso_now()
        self.sources = {
            "mic": SourceMetrics(role="mic"),
            "remote": SourceMetrics(role="remote"),
        }
        self.processing_history = deque(maxlen=8000)
        self.logs = deque(maxlen=400)
        self.last_write_at = "\u2014"
        self.running = False
        self.loading = False
        self.stopping = False
        self.model_loaded = False
        self.model_name = ""
        self.out_dir = ""
        self.server_error = ""
        self.process = psutil.Process() if psutil else None
        self.interval_count = 0
        self.total_words = 0
        self.last_interval_mic_text = ""
        self.last_interval_remote_text = ""

    def reset_for_run(self, model_name: str, out_dir: str):
        with self.lock:
            self.started_at = iso_now()
            self.processing_history.clear()
            self.logs.clear()
            self.last_write_at = "\u2014"
            self.server_error = ""
            self.model_name = model_name
            self.out_dir = out_dir
            self.stopping = False
            self.model_loaded = False
            self.interval_count = 0
            self.total_words = 0
            self.last_interval_mic_text = ""
            self.last_interval_remote_text = ""
            for role in self.sources:
                enabled = self.sources[role].enabled
                device_id = self.sources[role].device_id
                device_name = self.sources[role].device_name
                self.sources[role] = SourceMetrics(
                    role=role, enabled=enabled,
                    device_id=device_id, device_name=device_name,
                )

    def set_running(self, running: bool, loading: bool = False):
        with self.lock:
            self.running = running
            self.loading = loading
            if running:
                self.stopping = False

    def set_model_loaded(self):
        with self.lock:
            self.model_loaded = True
            self.loading = False

    def set_server_error(self, message: str):
        with self.lock:
            self.server_error = message
            self.loading = False
            self.running = False
            self.stopping = False

    def set_stopping(self, stopping: bool):
        with self.lock:
            self.stopping = stopping

    def set_source_config(self, role: str, enabled: bool, device_id: Optional[int], device_name: str):
        with self.lock:
            src = self.sources[role]
            src.enabled = enabled
            src.device_id = device_id
            src.device_name = device_name

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

    def record_interval(self, mic_text: str, remote_text: str, mic_words: int, remote_words: int):
        with self.lock:
            self.interval_count += 1
            total_w = mic_words + remote_words
            self.total_words += total_w
            self.last_write_at = iso_now()
            self.last_interval_mic_text = mic_text
            self.last_interval_remote_text = remote_text
            for role, text, words in [("mic", mic_text, mic_words), ("remote", remote_text, remote_words)]:
                if text:
                    src = self.sources[role]
                    src.words += words
                    src.last_text = text[:200]
                    src.last_commit_at = iso_now()
            if mic_text:
                self.logs.append({
                    "ts": time.time(), "role": "mic",
                    "words": mic_words, "language": "",
                    "text": mic_text[:200], "at": iso_now(),
                })
            if remote_text:
                self.logs.append({
                    "ts": time.time(), "role": "remote",
                    "words": remote_words, "language": "",
                    "text": remote_text[:200], "at": iso_now(),
                })

    def snapshot(self, out_dir: str, current_config: Optional[dict] = None):
        with self.lock:
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

            intervals_file = Path(out_dir) / "intervals.jsonl"
            interval_cfg = _interval_cfg(current_config or {})
            return {
                "session": {
                    "started_at": self.started_at,
                    "running": self.running,
                    "loading": self.loading,
                    "stopping": self.stopping,
                    "model_loaded": self.model_loaded,
                    "server_error": self.server_error,
                    "last_write_at": self.last_write_at,
                    "total_words": self.total_words,
                    "total_intervals": self.interval_count,
                    "intervals_file_size": file_size(intervals_file),
                    "min_interval_s": interval_cfg["min_interval_s"],
                    "max_interval_s": interval_cfg["max_interval_s"],
                    "silence_cut_ms": interval_cfg["silence_cut_ms"],
                },
                "sources": {k: asdict(v) for k, v in self.sources.items()},
                "last_interval": {
                    "mic_text": self.last_interval_mic_text,
                    "remote_text": self.last_interval_remote_text,
                },
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


# ---------------------------------------------------------------------------
# IntervalWriter \u2014 replaces JsonlWriter / FullAudioWriter
# ---------------------------------------------------------------------------

class IntervalWriter:
    """Append-only writer for intervals.jsonl."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / "intervals.jsonl"
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: dict):
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()

    def read_all(self) -> list[dict]:
        records = []
        if not self.path.exists():
            return records
        with self._lock:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records


# ---------------------------------------------------------------------------
# IntervalCutCoordinator \u2014 decides when to cut intervals
# ---------------------------------------------------------------------------

class IntervalCutCoordinator:
    """Tracks silence status across all channels and decides when to cut."""

    def __init__(self, cfg: dict):
        self.min_interval_s = float(cfg.get("min_interval_s", 300))
        self.max_interval_s = float(cfg.get("max_interval_s", 600))
        self.silence_cut_ms = float(cfg.get("silence_cut_ms", 2000))
        self._lock = threading.Lock()
        self._interval_start_mono = time.monotonic()
        self._interval_start_dt = datetime.now().astimezone()
        self._silence_start_mono: Optional[float] = None
        self._channel_silence = {}  # role -> bool (is silent)
        self._cut_requested = False
        self._cut_at_sample_offset: Optional[int] = None
        self._cut_at_dt: Optional[datetime] = None

    def register_channel(self, role: str):
        with self._lock:
            self._channel_silence[role] = True

    def report_silence(self, role: str, is_silent: bool, frame_mono_time: float):
        with self._lock:
            if self._cut_requested:
                return
            self._channel_silence[role] = is_silent
            elapsed = frame_mono_time - self._interval_start_mono
            all_silent = all(self._channel_silence.values()) if self._channel_silence else False

            # Force cut at max_interval
            if elapsed >= self.max_interval_s:
                self._trigger_cut_locked(frame_mono_time)
                return

            if elapsed < self.min_interval_s:
                # Before min_interval, just track silence state
                if all_silent:
                    if self._silence_start_mono is None:
                        self._silence_start_mono = frame_mono_time
                else:
                    self._silence_start_mono = None
                return

            # Between min and max: wait for silence gap
            if all_silent:
                if self._silence_start_mono is None:
                    self._silence_start_mono = frame_mono_time
                else:
                    silence_duration_ms = (frame_mono_time - self._silence_start_mono) * 1000.0
                    if silence_duration_ms >= self.silence_cut_ms:
                        # Cut at center of silence gap
                        cut_mono = self._silence_start_mono + (self.silence_cut_ms / 2000.0)
                        self._trigger_cut_locked(cut_mono)
            else:
                self._silence_start_mono = None

    def _trigger_cut_locked(self, cut_mono_time: float):
        self._cut_requested = True
        elapsed_from_start = cut_mono_time - self._interval_start_mono
        self._cut_at_sample_offset = int(round(elapsed_from_start * TARGET_SAMPLE_RATE))
        self._cut_at_dt = self._interval_start_dt + timedelta(seconds=elapsed_from_start)

    def check_cut(self) -> Optional[tuple[int, datetime, datetime]]:
        """Returns (sample_offset, interval_start_dt, cut_dt) if a cut is pending."""
        with self._lock:
            if not self._cut_requested:
                return None
            result = (
                self._cut_at_sample_offset,
                self._interval_start_dt,
                self._cut_at_dt,
            )
            # Reset for next interval
            self._cut_requested = False
            self._interval_start_mono = time.monotonic()
            self._interval_start_dt = self._cut_at_dt
            self._silence_start_mono = None
            self._cut_at_sample_offset = None
            self._cut_at_dt = None
            return result

    def current_interval_info(self) -> dict:
        """Return info about the current (in-progress) interval."""
        with self._lock:
            elapsed = time.monotonic() - self._interval_start_mono
            all_silent = all(self._channel_silence.values()) if self._channel_silence else False
            return {
                "start_at": self._interval_start_dt.isoformat(timespec="seconds"),
                "elapsed_s": round(elapsed, 1),
                "channels_silent": all_silent,
            }

    def flush_current(self) -> Optional[tuple[int, datetime, datetime]]:
        """Force-cut at current position (for graceful stop)."""
        with self._lock:
            now_mono = time.monotonic()
            elapsed = now_mono - self._interval_start_mono
            if elapsed < 0.5:  # less than 0.5s of audio, skip
                return None
            sample_offset = int(round(elapsed * TARGET_SAMPLE_RATE))
            cut_dt = self._interval_start_dt + timedelta(seconds=elapsed)
            return (sample_offset, self._interval_start_dt, cut_dt)


# ---------------------------------------------------------------------------
# AudioStreamWorker \u2014 simplified, no per-utterance VAD
# ---------------------------------------------------------------------------

class AudioStreamWorker(threading.Thread):
    """Captures audio from a device, resamples to 16kHz mono, detects silence,
    and accumulates audio in a buffer. The IntervalCutCoordinator decides cuts."""

    def __init__(self, role: str, device: Optional[int],
                 coordinator: IntervalCutCoordinator,
                 metrics: MetricsStore,
                 stop_event: threading.Event,
                 cfg: dict):
        super().__init__(daemon=True)
        self.role = role
        self.device = device
        self.coordinator = coordinator
        self.metrics = metrics
        self.stop_event = stop_event
        self.cfg = cfg
        self.vad = webrtcvad.Vad(1)
        self.q: queue.Queue[tuple[datetime, np.ndarray]] = queue.Queue(
            maxsize=cfg.get("audio_queue_size", 2048)
        )
        self.stream: Optional[sd.InputStream] = None
        self.enabled = device is not None
        self.input_sr = TARGET_SAMPLE_RATE
        self.input_channels = 1
        self.frame_samples = TARGET_SAMPLE_RATE * FRAME_MS // 1000
        self._partial_buf = np.zeros(0, dtype=np.int16)
        # Growing buffer of 16kHz mono int16 samples for current interval
        self._audio_buffer: list[np.ndarray] = []
        self._speech_flags: list[bool] = []
        self._speech_vad_flags: list[bool] = []
        self._speech_rms_flags: list[bool] = []
        self._buffer_lock = threading.Lock()
        self.coordinator.register_channel(role)
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
        self.frame_samples = TARGET_SAMPLE_RATE * FRAME_MS // 1000
        self.stream = sd.InputStream(
            samplerate=self.input_sr,
            blocksize=max(1, int(round(self.input_sr * FRAME_MS / 1000.0))),
            channels=self.input_channels,
            dtype=DTYPE,
            device=self.device,
            callback=self.callback,
            latency="low",
        )
        self.stream.start()
        self.metrics.update_source(self.role, status="listening")

    def _to_mono_16k_i16(self, audio: np.ndarray) -> np.ndarray:
        mono = audio[:, 0] if audio.ndim == 2 and audio.shape[1] == 1 else (
            audio.mean(axis=1) if audio.ndim == 2 else audio
        )
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

    def _process_frame(self, mono16: np.ndarray):
        """Append frame to buffer and report silence status."""
        now_mono = time.monotonic()
        # Silence detection: VAD + RMS fallback
        try:
            vad_speech = self.vad.is_speech(mono16.tobytes(), TARGET_SAMPLE_RATE)
        except Exception:
            vad_speech = False
        rms_speech = (not vad_speech) and self._frame_rms(mono16) >= RMS_SPEECH_THRESHOLD
        is_speech = vad_speech or rms_speech
        is_silent = not is_speech

        with self._buffer_lock:
            self._audio_buffer.append(mono16.copy())
            self._speech_flags.append(bool(is_speech))
            self._speech_vad_flags.append(bool(vad_speech))
            self._speech_rms_flags.append(bool(rms_speech))

        self.coordinator.report_silence(self.role, is_silent, now_mono)

    def split_buffer(self, sample_offset: int) -> np.ndarray:
        """Split buffer at sample_offset, return the first part, keep the rest."""
        with self._buffer_lock:
            if not self._audio_buffer:
                return np.zeros(0, dtype=np.int16)
            full = np.concatenate(self._audio_buffer)
            offset = min(sample_offset, len(full))
            completed = full[:offset]
            remainder = full[offset:]
            frame_offset = min(len(self._speech_flags), int(round(offset / max(1, self.frame_samples))))
            self._audio_buffer = [remainder] if len(remainder) > 0 else []
            self._speech_flags = self._speech_flags[frame_offset:]
            self._speech_vad_flags = self._speech_vad_flags[frame_offset:]
            self._speech_rms_flags = self._speech_rms_flags[frame_offset:]
            return completed

    def flush_buffer(self) -> np.ndarray:
        """Return all buffered audio."""
        with self._buffer_lock:
            if not self._audio_buffer:
                return np.zeros(0, dtype=np.int16)
            full = np.concatenate(self._audio_buffer)
            self._audio_buffer = []
            self._speech_flags = []
            self._speech_vad_flags = []
            self._speech_rms_flags = []
            return full

    def current_speech_frames(self) -> int:
        with self._buffer_lock:
            return int(sum(self._speech_flags))

    def current_speech_stats(self) -> dict:
        with self._buffer_lock:
            speech_frames = int(sum(self._speech_flags))
            vad_frames = int(sum(self._speech_vad_flags))
            rms_frames = int(sum(self._speech_rms_flags))
        return {
            "speech_frames": speech_frames,
            "speech_seconds": round(speech_frames * (FRAME_MS / 1000.0), 1),
            "vad_frames": vad_frames,
            "vad_seconds": round(vad_frames * (FRAME_MS / 1000.0), 1),
            "rms_frames": rms_frames,
            "rms_seconds": round(rms_frames * (FRAME_MS / 1000.0), 1),
            "rms_threshold": RMS_SPEECH_THRESHOLD,
        }

    def run(self):
        if not self.enabled:
            return
        try:
            self.open_stream()
        except Exception as e:
            self.metrics.update_source(self.role, status="error", last_error=str(e))
            return
        try:
            while not self.stop_event.is_set():
                self.metrics.update_source(self.role, queue_size=self.q.qsize())
                try:
                    captured_at, chunk = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                mono16 = self._to_mono_16k_i16(chunk)
                if len(self._partial_buf) > 0:
                    mono16 = np.concatenate([self._partial_buf, mono16])
                    self._partial_buf = np.zeros(0, dtype=np.int16)
                while len(mono16) >= self.frame_samples:
                    frame = mono16[:self.frame_samples]
                    mono16 = mono16[self.frame_samples:]
                    self._process_frame(frame)
                if len(mono16) > 0:
                    self._partial_buf = mono16.copy()
        finally:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
            self.metrics.update_source(self.role, status="stopped", busy=False)


# ---------------------------------------------------------------------------
# Background transcription
# ---------------------------------------------------------------------------

def _transcribe_audio_chunk(
    backend: ASRBackend,
    backend_lock: threading.Lock,
    audio_i16: np.ndarray,
    language: str,
) -> str:
    """Transcribe an int16 audio array. Splits into ~30s chunks internally."""
    chunk_samples = max(1, int(_TRANSCRIBE_CHUNK_S * TARGET_SAMPLE_RATE))
    parts: list[str] = []

    for chunk_start in range(0, len(audio_i16), chunk_samples):
        chunk = audio_i16[chunk_start:chunk_start + chunk_samples]
        if len(chunk) == 0 or not np.any(chunk):
            continue
        audio_f32 = chunk.astype(np.float32) / 32768.0

        def call_backend(lang):
            with backend_lock:
                result = backend.transcribe_once(audio_f32, lang)
            result["text"] = clean_transcribed_text(result.get("text", ""))
            return result

        try:
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
        except Exception:
            continue

        best = pick_best_candidate(candidates)
        if best and best.get("text"):
            parts.append(best["text"])

    return " ".join(parts)


# ---------------------------------------------------------------------------
# TranscriberController \u2014 the main controller
# ---------------------------------------------------------------------------

class TranscriberController:
    def __init__(self, model_manager=None):
        self.metrics = MetricsStore()
        self.stop_event = None
        self.backend = None
        self.backend_lock = threading.Lock()
        self.interval_writer: Optional[IntervalWriter] = None
        self.coordinator: Optional[IntervalCutCoordinator] = None
        self.workers: list[AudioStreamWorker] = []
        self.controller_lock = threading.Lock()
        self.runner_thread = None
        self.current_config = {}
        self.model_manager = model_manager
        self._bg_queue: queue.Queue = queue.Queue(maxsize=64)
        self._bg_thread: Optional[threading.Thread] = None

    def start(self, config: dict):
        with self.controller_lock:
            if self.runner_thread and self.runner_thread.is_alive():
                if self.stop_event and self.stop_event.is_set():
                    self.runner_thread.join(timeout=15)
                    if self.runner_thread.is_alive():
                        return False, "\u0418\u0434\u0451\u0442 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430, \u043f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435."
                else:
                    return False, "\u0422\u0440\u0430\u043d\u0441\u043a\u0440\u0438\u0431\u0430\u0446\u0438\u044f \u0443\u0436\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430."
            ok, model_value = model_is_valid(config["model"])
            if not ok:
                return False, model_value
            if self.model_manager and self.model_manager.is_busy(model_value):
                return False, "\u041c\u043e\u0434\u0435\u043b\u044c \u0443\u0436\u0435 \u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u0442\u0441\u044f."
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
            return True, "\u0417\u0430\u043f\u0443\u0441\u043a..."

    def _run(self):
        cfg = self.current_config
        icfg = _interval_cfg(cfg)
        out_dir = Path(cfg["out_dir"]).expanduser()
        self.metrics.reset_for_run(cfg["model"], str(out_dir))
        self.metrics.set_running(True, loading=True)
        try:
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
                self.metrics.set_server_error(f"\u041e\u0448\u0438\u0431\u043a\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0438 \u043c\u043e\u0434\u0435\u043b\u0438: {e}")
                return
            if self.model_manager:
                self.model_manager.finish(cfg["model"], None)

            self.stop_event = threading.Event()
            self.interval_writer = IntervalWriter(out_dir)
            self.coordinator = IntervalCutCoordinator(icfg)
            self._bg_queue = queue.Queue(maxsize=64)

            init_tracker(out_dir)

            devices = {d["id"]: d["name"] for d in list_input_devices()}
            self.metrics.set_source_config(
                "mic", cfg.get("mic_device") is not None,
                cfg.get("mic_device"), devices.get(cfg.get("mic_device"), "\u2014"),
            )
            self.metrics.set_source_config(
                "remote", cfg.get("remote_device") is not None,
                cfg.get("remote_device"), devices.get(cfg.get("remote_device"), "\u2014"),
            )

            self.workers = [
                AudioStreamWorker("mic", cfg.get("mic_device"), self.coordinator, self.metrics, self.stop_event, icfg),
                AudioStreamWorker("remote", cfg.get("remote_device"), self.coordinator, self.metrics, self.stop_event, icfg),
            ]
            for w in self.workers:
                if w.enabled:
                    w.start()

            self._bg_thread = threading.Thread(target=self._background_transcribe_worker, daemon=True)
            self._bg_thread.start()

            while not self.stop_event.is_set():
                time.sleep(0.1)
                cut = self.coordinator.check_cut()
                if cut is not None:
                    sample_offset, start_dt, end_dt = cut
                    audio_chunks = {}
                    for w in self.workers:
                        if w.enabled:
                            audio_chunks[w.role] = w.split_buffer(sample_offset)
                    self._bg_queue.put((start_dt, end_dt, audio_chunks))
        finally:
            flush = self.coordinator.flush_current() if self.coordinator else None
            if flush and self.backend is not None:
                sample_offset, start_dt, end_dt = flush
                audio_chunks = {}
                for w in self.workers:
                    if w.enabled:
                        audio_chunks[w.role] = w.split_buffer(sample_offset)
                self._transcribe_interval(start_dt, end_dt, audio_chunks)
            for w in self.workers:
                if w.enabled:
                    w.join(timeout=180)
            try:
                self._bg_queue.put_nowait(None)
            except queue.Full:
                pass
            if self._bg_thread:
                self._bg_thread.join(timeout=120)
            self.workers = []
            self.coordinator = None
            self.interval_writer = None
            self.backend = None
            self._bg_thread = None
            self.stop_event = None
            self.metrics.set_running(False, loading=False)
            self.metrics.set_stopping(False)
            self.runner_thread = None

    def _background_transcribe_worker(self):
        """Drain bg_queue and transcribe intervals."""
        while True:
            try:
                item = self._bg_queue.get(timeout=1.0)
            except queue.Empty:
                if self.stop_event and self.stop_event.is_set():
                    break
                continue
            if item is None:
                break
            start_dt, end_dt, audio_chunks = item
            self._transcribe_interval(start_dt, end_dt, audio_chunks)

    def _transcribe_interval(self, start_dt: datetime, end_dt: datetime, audio_chunks: dict):
        """Transcribe audio from all channels for one interval."""
        language = str(self.current_config.get("language") or "auto")
        duration_s = (end_dt - start_dt).total_seconds()
        results = {}

        for role in ("mic", "remote"):
            audio = audio_chunks.get(role)
            if audio is None or len(audio) == 0:
                results[role] = {"text": "", "language": "", "words": 0}
                continue

            self.metrics.update_source(role, busy=True, status="processing")
            audio_sec = len(audio) / TARGET_SAMPLE_RATE
            t0 = time.monotonic()
            try:
                text = _transcribe_audio_chunk(
                    self.backend, self.backend_lock, audio, language,
                )
            except Exception as e:
                self.metrics.update_source(
                    role, busy=False, status="listening",
                    last_error=str(e),
                )
                results[role] = {"text": "", "language": "", "words": 0}
                continue
            proc_sec = time.monotonic() - t0
            lag = max(0.0, proc_sec - audio_sec)
            self.metrics.record_processing(role, audio_sec, proc_sec, lag)
            self.metrics.update_source(role, busy=False, status="listening")

            words = count_words(text) if text else 0
            results[role] = {"text": text, "language": language, "words": words}

            # Free audio memory
            del audio

        # Write to intervals.jsonl
        mic_text = results.get("mic", {}).get("text", "")
        remote_text = results.get("remote", {}).get("text", "")
        mic_words = results.get("mic", {}).get("words", 0)
        remote_words = results.get("remote", {}).get("words", 0)

        # Log voice triggers
        if mic_words > 0:
            log_voice_trigger("mic", start_dt)
        if remote_words > 0:
            log_voice_trigger("remote", start_dt)

        record = {
            "type": "interval",
            "start_at": start_dt.isoformat(timespec="seconds"),
            "end_at": end_dt.isoformat(timespec="seconds"),
            "duration_s": round(duration_s, 1),
            "mic_text": mic_text,
            "remote_text": remote_text,
            "mic_words": mic_words,
            "remote_words": remote_words,
            "mic_language": results.get("mic", {}).get("language", ""),
            "remote_language": results.get("remote", {}).get("language", ""),
        }
        if self.interval_writer:
            self.interval_writer.write(record)

        self.metrics.record_interval(mic_text, remote_text, mic_words, remote_words)

    def stop(self):
        with self.controller_lock:
            if self.stop_event and not self.stop_event.is_set():
                self.metrics.set_stopping(True)
                self.stop_event.set()
                return True, "\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0448\u0435\u043d\u0430."
            return False, "\u0422\u0440\u0430\u043d\u0441\u043a\u0440\u0438\u0431\u0430\u0446\u0438\u044f \u0443\u0436\u0435 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430."

    def get_current_interval(self) -> Optional[dict]:
        if self.coordinator and self.metrics.running:
            info = self.coordinator.current_interval_info()
            speech_frames = 0
            speech_by_channel = {}
            vad_frames = 0
            rms_frames = 0
            for worker in self.workers:
                if worker.enabled:
                    stats = worker.current_speech_stats()
                    speech_by_channel[worker.role] = stats
                    speech_frames += stats["speech_frames"]
                    vad_frames += stats["vad_frames"]
                    rms_frames += stats["rms_frames"]
            info["speech_frames_count"] = speech_frames
            info["speech_seconds"] = round(speech_frames * (FRAME_MS / 1000.0), 1)
            info["vad_frames_count"] = vad_frames
            info["vad_seconds"] = round(vad_frames * (FRAME_MS / 1000.0), 1)
            info["rms_frames_count"] = rms_frames
            info["rms_seconds"] = round(rms_frames * (FRAME_MS / 1000.0), 1)
            info["rms_threshold"] = RMS_SPEECH_THRESHOLD
            info["speech_by_channel"] = speech_by_channel
            return info
        return None

    def state(self):
        out_dir = self.current_config.get("out_dir", "./transcripts")
        return self.metrics.snapshot(str(Path(out_dir).expanduser()), current_config=self.current_config)
