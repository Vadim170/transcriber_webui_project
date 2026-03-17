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
from pywhispercpp.model import Model

try:
    import psutil
except Exception:
    psutil = None

from .config import FALLBACK_MODELS

TARGET_SAMPLE_RATE = 16000
DTYPE = "int16"
FRAME_MS = 30
MIN_SPEECH_MS = 200
MAX_UTTERANCE_MS = 8000
SILENCE_TO_COMMIT_MS = 450
VAD_AGGRESSIVENESS = 2
MIN_TEXT_CHARS = 2
MIN_RMS_UTTERANCE = 0.006
MIN_RMS_FRAME_FALLBACK = 0.012
PREROLL_MS = 180
COMBINED_SPLIT_GAP_S = 5.0
COMBINED_FLUSH_IDLE_S = 5.0

GARBAGE_PATTERNS = [
    re.compile(r"^\[(?:BLANK_AUDIO|Ambient|Motor|Noise|Music|Applause|M|S|Speech|Silence)\]$", re.I),
    re.compile(r"^\[[^\]]{1,24}\]$"),
]


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


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
    p = Path(str(model_text)).expanduser()
    if p.exists() and p.is_file():
        return True, str(p)
    if model_text in FALLBACK_MODELS:
        return True, model_text
    return False, "Модель должна быть либо известным именем pywhispercpp, либо существующим путём к .bin файлу."


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

    def reset_for_run(self, model_name: str, out_dir: str):
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
                "t": time.time(),
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


class AudioStreamWorker(threading.Thread):
    def __init__(self, role: str, device: Optional[int], model: Model, writer: JsonlWriter, metrics: MetricsStore, stop_event: threading.Event, session_started_at: datetime):
        super().__init__(daemon=True)
        self.role = role
        self.device = device
        self.model = model
        self.writer = writer
        self.metrics = metrics
        self.stop_event = stop_event
        self.session_started_at = session_started_at
        self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self.q: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
        self.stream: Optional[sd.InputStream] = None
        self.start_monotonic = time.monotonic()
        self.current_frames = []
        self.current_start_s: Optional[float] = None
        self.last_speech_ts: Optional[float] = None
        self.enabled = device is not None
        self.input_sr = TARGET_SAMPLE_RATE
        self.input_channels = 1
        self.frame_samples = TARGET_SAMPLE_RATE * FRAME_MS // 1000
        self.preroll_frames: deque[np.ndarray] = deque(maxlen=max(1, PREROLL_MS // FRAME_MS))
        self.lag_estimate_sec = 0.0
        self.metrics.update_source(self.role, status="ready")

    def callback(self, indata, frames, time_info, status):
        audio = np.array(indata, copy=True)
        try:
            self.q.put_nowait(audio)
        except queue.Full:
            src = self.metrics.sources[self.role]
            self.metrics.update_source(self.role, dropped_chunks=src.dropped_chunks + 1)

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
            score += cyr * 4 - lat * 2
        elif lang == "en":
            score += lat * 4 - cyr * 2
        else:
            score += max(cyr, lat) * 2
        if len(text.split()) >= 2:
            score += 10
        return score

    def _transcribe_once(self, media, forced_language: Optional[str]):
        kwargs = dict(language=forced_language, translate=False, no_context=True, print_realtime=False, print_progress=False, print_timestamps=False, suppress_blank=True, no_speech_thold=0.6)
        try:
            segments = self.model.transcribe(media, **kwargs)
        except TypeError:
            kwargs.pop("suppress_blank", None)
            segments = self.model.transcribe(media, **kwargs)
        parts, detected_language, seg_end = [], forced_language, None
        for seg in segments:
            text = getattr(seg, "text", "")
            if text:
                parts.append(text)
            lang = getattr(seg, "language", None)
            if lang:
                detected_language = lang
            t1 = getattr(seg, "t1", None)
            if t1 is not None:
                try:
                    seg_end = float(t1) / 100.0
                except Exception:
                    pass
        return {"text": self._clean_text(" ".join(parts)), "language": detected_language, "seg_end_rel": seg_end}

    def _pick_best_candidate(self, candidates):
        best = None
        best_score = -10_000_000
        for cand in candidates:
            lang = cand["language"] or "auto"
            score = self._score_text_for_lang(cand["text"], lang)
            if lang in ("ru", "en"):
                score += 5
            if score > best_score:
                best_score = score
                best = cand
        return None if best is None or self._is_garbage_text(best["text"]) else best

    def transcribe_audio(self, audio: np.ndarray, start_s: float, end_s: float):
        audio_sec = len(audio) / TARGET_SAMPLE_RATE
        audio_f32 = audio.astype(np.float32) / 32768.0
        t0 = time.monotonic()
        self.metrics.update_source(self.role, busy=True, status="processing")
        try:
            auto_res = self._transcribe_once(audio_f32, None)
            ru_res = self._transcribe_once(audio_f32, "ru")
            en_res = self._transcribe_once(audio_f32, "en")
        except Exception as e:
            self.metrics.update_source(self.role, busy=False, status="listening", last_error=str(e))
            return
        proc_sec = time.monotonic() - t0
        self.lag_estimate_sec = max(0.0, self.lag_estimate_sec + proc_sec - audio_sec)
        self.metrics.record_processing(self.role, audio_sec, proc_sec, self.lag_estimate_sec)
        best = self._pick_best_candidate([auto_res, ru_res, en_res])
        self.metrics.update_source(self.role, busy=False, status="listening")
        if not best:
            return
        seg_end = start_s + best["seg_end_rel"] if best["seg_end_rel"] is not None else end_s
        utt = Utterance(role=self.role, text=best["text"], start_s=start_s, end_s=seg_end, start_at=self._iso_at(start_s), end_at=self._iso_at(seg_end), language=best["language"])
        self.writer.write(utt)
        self.metrics.update_source(self.role, last_language=utt.language or "—", last_text=utt.text, last_commit_at=utt.end_at)

    def commit_current(self, end_s: float):
        if not self.current_frames or self.current_start_s is None:
            self.reset_current(); return
        audio = np.concatenate(self.current_frames)
        if len(audio) == 0:
            self.reset_current(); return
        duration_ms = len(audio) / TARGET_SAMPLE_RATE * 1000.0
        if duration_ms < MIN_SPEECH_MS:
            self.reset_current(); return
        if self._frame_rms(audio) < MIN_RMS_UTTERANCE:
            self.reset_current(); return
        self.transcribe_audio(audio, self.current_start_s, end_s)
        self.reset_current()

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
                self.metrics.update_source(self.role, queue_size=self.q.qsize(), current_utterance_ms=round(self.current_duration_ms(), 1), current_buffer_ms=round(self.current_duration_ms(), 1))
                try:
                    chunk = self.q.get(timeout=0.2)
                except queue.Empty:
                    if self.current_frames and self.last_speech_ts is not None:
                        now_s = time.monotonic() - self.start_monotonic
                        if (now_s - self.last_speech_ts) * 1000.0 >= SILENCE_TO_COMMIT_MS:
                            self.commit_current(now_s)
                    continue
                mono16 = self._to_mono_16k_i16(chunk)
                if len(mono16) < self.frame_samples:
                    continue
                if len(mono16) > self.frame_samples:
                    mono16 = mono16[: self.frame_samples]
                self.preroll_frames.append(mono16.copy())
                now_s = time.monotonic() - self.start_monotonic
                rms = self._frame_rms(mono16)
                try:
                    is_speech = self.vad.is_speech(mono16.tobytes(), TARGET_SAMPLE_RATE)
                except Exception:
                    is_speech = False
                if not is_speech and rms >= MIN_RMS_FRAME_FALLBACK:
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
                    if self.current_duration_ms() >= MAX_UTTERANCE_MS:
                        self.commit_current(now_s)
                else:
                    if self.current_frames:
                        self.current_frames.append(mono16)
                        if self.last_speech_ts is not None and (now_s - self.last_speech_ts) * 1000.0 >= SILENCE_TO_COMMIT_MS:
                            self.commit_current(now_s)
        finally:
            if self.current_frames:
                self.commit_current(time.monotonic() - self.start_monotonic)
            if self.stream is not None:
                self.stream.stop(); self.stream.close()
            self.metrics.update_source(self.role, status="stopped", busy=False)


class TranscriberController:
    def __init__(self):
        self.metrics = MetricsStore()
        self.stop_event = None
        self.model = None
        self.writer = None
        self.workers = []
        self.controller_lock = threading.Lock()
        self.runner_thread = None
        self.current_config = {}

    def start(self, config: dict):
        with self.controller_lock:
            if self.runner_thread and self.runner_thread.is_alive():
                return False, "Транскрибация уже запущена."
            ok, model_value = model_is_valid(config["model"])
            if not ok:
                return False, model_value
            self.current_config = config.copy()
            self.current_config["model"] = model_value
            self.runner_thread = threading.Thread(target=self._run, daemon=True)
            self.runner_thread.start()
            return True, "Запуск..."

    def _run(self):
        cfg = self.current_config
        self.metrics.reset_for_run(cfg["model"], str(Path(cfg["out_dir"]).expanduser()))
        self.metrics.set_running(True, loading=True)
        try:
            self.model = Model(cfg["model"], n_threads=int(cfg["threads"]))
            self.metrics.set_model_loaded()
        except Exception as e:
            self.metrics.set_server_error(f"Ошибка загрузки модели: {e}")
            return
        self.stop_event = threading.Event()
        session_started_at = datetime.now().astimezone()
        out_dir = Path(cfg["out_dir"]).expanduser()
        self.writer = JsonlWriter(out_dir, self.metrics)
        devices = {d["id"]: d["name"] for d in list_input_devices()}
        self.metrics.set_source_config("mic", cfg.get("mic_device") is not None, cfg.get("mic_device"), devices.get(cfg.get("mic_device"), "—"))
        self.metrics.set_source_config("remote", cfg.get("remote_device") is not None, cfg.get("remote_device"), devices.get(cfg.get("remote_device"), "—"))
        self.workers = [
            AudioStreamWorker("mic", cfg.get("mic_device"), self.model, self.writer, self.metrics, self.stop_event, session_started_at),
            AudioStreamWorker("remote", cfg.get("remote_device"), self.model, self.writer, self.metrics, self.stop_event, session_started_at),
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
                    w.join(timeout=3)
            if self.writer:
                self.writer.close()
            self.metrics.set_running(False, loading=False)

    def stop(self):
        with self.controller_lock:
            if self.stop_event:
                self.stop_event.set()
            return True, "Остановка запрошена."

    def state(self):
        out_dir = self.current_config.get("out_dir", "./transcripts")
        return self.metrics.snapshot(str(Path(out_dir).expanduser()))
