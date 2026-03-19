"""ASR backend abstraction layer."""

import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

TARGET_SAMPLE_RATE = 16000
QUANTIZATION_MODES = ("none", "4bit", "8bit")

KNOWN_MODELS: dict[str, dict] = {
    "mistralai/Voxtral-Mini-3B-2507": {
        "backend": "voxtral",
        "label": "Voxtral Mini 3B",
        "size": "~6 GB",
        "note": "transformers · torch · accelerate",
        "quantization": ["none", "4bit", "8bit"],
    },
    "mistralai/Voxtral-Small-24B-2507": {
        "backend": "voxtral",
        "label": "Voxtral Small 24B",
        "size": "~48 GB",
        "note": "transformers · torch · accelerate",
        "quantization": ["none", "4bit", "8bit"],
    },
    "nvidia/canary-1b-v2": {
        "backend": "nemo_asr",
        "label": "NVIDIA Canary 1B v2",
        "size": "~2 GB",
        "note": "NeMo ASR · multilingual",
        "quantization": ["none"],
    },
    "nvidia/parakeet-tdt-1.1b": {
        "backend": "nemo_asr",
        "label": "NVIDIA Parakeet TDT 1.1B",
        "size": "~4.4 GB",
        "note": "NeMo ASR · English",
        "quantization": ["none"],
    },
    "nvidia/parakeet-tdt-0.6b-v2": {
        "backend": "nemo_asr",
        "label": "NVIDIA Parakeet TDT 0.6B v2",
        "size": "~2.5 GB",
        "note": "NeMo ASR · English",
        "quantization": ["none"],
    },
    "nvidia/parakeet-tdt-0.6b-v3": {
        "backend": "nemo_asr",
        "label": "NVIDIA Parakeet TDT 0.6B v3",
        "size": "~2.5 GB",
        "note": "NeMo ASR · multilingual",
        "quantization": ["none"],
    },
    "FluidInference/parakeet-tdt-0.6b-v3-coreml": {
        "backend": "macos_parakeet",
        "label": "Parakeet TDT 0.6B v3 CoreML",
        "size": "~2.5 GB",
        "note": "macOS fast path · Apple Silicon",
        "quantization": ["none"],
    },
    "Qwen/Qwen3-ASR-1.7B": {
        "backend": "qwen_asr",
        "label": "Qwen3-ASR 1.7B",
        "size": "~3.4 GB",
        "note": "qwen-asr · torch",
        "quantization": ["none", "4bit", "8bit"],
    },
    "Qwen/Qwen3-ASR-0.6B": {
        "backend": "qwen_asr",
        "label": "Qwen3-ASR 0.6B",
        "size": "~1.2 GB",
        "note": "qwen-asr · torch",
        "quantization": ["none", "4bit", "8bit"],
    },
}


def _write_temp_wav_16k(audio_f32: np.ndarray) -> str:
    """Write float32 mono 16 kHz audio to a temporary WAV file. Returns path."""
    pcm16 = np.clip(audio_f32 * 32767, -32768, 32767).astype(np.int16)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())
    return path


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ASRBackend(ABC):
    """Uniform interface every ASR backend must implement."""

    @abstractmethod
    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        """Download / initialise the model. Called once from the controller thread."""

    @abstractmethod
    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        """Transcribe a chunk of mono float32 16 kHz audio.

        Returns ``{"text": str, "language": str|None, "seg_end_rel": float|None}``.
        ``seg_end_rel`` is the last-segment end time relative to the start of
        the chunk (seconds) when available, otherwise ``None``.
        """

    @property
    def supports_multi_candidate(self) -> bool:
        """When True the worker will try auto / ru / en and pick the best."""
        return False

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        """Return a user-facing error string if environment is unsupported."""
        return None


# ---------------------------------------------------------------------------
# whisper.cpp  (pywhispercpp)
# ---------------------------------------------------------------------------

class WhisperCppBackend(ASRBackend):
    supports_multi_candidate = True  # type: ignore[assignment]

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        if quantization != "none":
            return "Для whisper.cpp выберите quantization=none и используйте q5/q8 модель по имени."
        if importlib.util.find_spec("pywhispercpp.model") is None:
            return "Не найден pywhispercpp. Установите зависимости проекта."
        return None

    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        from pywhispercpp.model import Model
        if quantization != "none":
            raise RuntimeError("Для whisper.cpp выберите quantization=none и используйте q5/q8 модель по имени.")
        self._model = Model(model_id, n_threads=n_threads)

    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        kwargs = dict(
            language=language, translate=False, no_context=True,
            print_realtime=False, print_progress=False,
            print_timestamps=False, suppress_blank=True,
            no_speech_thold=0.6,
        )
        try:
            segments = self._model.transcribe(audio_f32_16k, **kwargs)
        except TypeError:
            kwargs.pop("suppress_blank", None)
            segments = self._model.transcribe(audio_f32_16k, **kwargs)

        parts: list[str] = []
        detected_language = language
        seg_end: Optional[float] = None
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
        return {
            "text": " ".join(parts),
            "language": detected_language,
            "seg_end_rel": seg_end,
        }


# ---------------------------------------------------------------------------
# Voxtral  (transformers)
# ---------------------------------------------------------------------------

VOXTRAL_LANG_MAP = {
    "en": "en", "es": "es", "fr": "fr", "pt": "pt",
    "hi": "hi", "de": "de", "nl": "nl", "it": "it",
}


class VoxtralBackend(ASRBackend):

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        if importlib.util.find_spec("torch") is None:
            return "Не найден torch. Установите: pip install torch transformers accelerate"
        if importlib.util.find_spec("transformers") is None:
            return "Не найден transformers. Установите: pip install torch transformers accelerate"
        if importlib.util.find_spec("accelerate") is None:
            return "Не найден accelerate. Установите: pip install torch transformers accelerate"
        if quantization != "none":
            if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
                return "4bit/8bit для Voxtral сейчас поддержаны только на CUDA/Linux через bitsandbytes."
            if importlib.util.find_spec("bitsandbytes") is None:
                return "Для 4bit/8bit установите bitsandbytes: pip install bitsandbytes"
        return None

    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        try:
            import torch
        except ModuleNotFoundError as e:
            raise RuntimeError("Не найден torch. Установите: pip install torch transformers accelerate") from e
        from transformers import AutoProcessor, VoxtralForConditionalGeneration
        model_kwargs = _hf_model_kwargs(torch, quantization)

        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        if quantization != "none" and device != "cuda":
            raise RuntimeError("4bit/8bit для Voxtral сейчас поддержаны только на CUDA через bitsandbytes.")

        dtype = torch.bfloat16 if device != "cpu" else torch.float32
        logger.info("Voxtral: loading %s on %s (%s)", model_id, device, dtype)
        self._processor = AutoProcessor.from_pretrained(model_id)
        self._model = VoxtralForConditionalGeneration.from_pretrained(
            model_id,
            torch_dtype=dtype,
            device_map=device,
            **model_kwargs,
        )
        self._device = device
        self._dtype = dtype
        self._model_id = model_id

    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        import torch

        wav_path = _write_temp_wav_16k(audio_f32_16k)
        try:
            kwargs: dict = {"audio": wav_path, "model_id": self._model_id}
            if language and language in VOXTRAL_LANG_MAP:
                kwargs["language"] = VOXTRAL_LANG_MAP[language]
            inputs = self._processor.apply_transcription_request(**kwargs)
            inputs = inputs.to(self._device, dtype=self._dtype)
            with torch.no_grad():
                outputs = self._model.generate(**inputs, max_new_tokens=500)
            text = self._processor.batch_decode(
                outputs[:, inputs.input_ids.shape[1]:],
                skip_special_tokens=True,
            )[0]
            return {"text": text.strip(), "language": language, "seg_end_rel": None}
        finally:
            os.unlink(wav_path)


# ---------------------------------------------------------------------------
# NVIDIA NeMo ASR  (Canary / Parakeet)
# ---------------------------------------------------------------------------

CANARY_SUPPORTED_LANGS = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr",
    "de", "el", "hu", "it", "lv", "lt", "mt", "pl", "pt",
    "ro", "sk", "sl", "es", "sv", "ru", "uk",
}


class NemoASRBackend(ASRBackend):

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        if quantization != "none":
            return "4bit/8bit для NeMo-моделей (Canary/Parakeet) пока не добавлены."
        if importlib.util.find_spec("torch") is None:
            return 'Не найден torch. Установите: pip install torch "nemo_toolkit[asr]>=2.2"'
        if importlib.util.find_spec("nemo") is None:
            return 'Не найден NeMo. Установите: pip install torch "nemo_toolkit[asr]>=2.2"'
        return None

    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        if quantization != "none":
            raise RuntimeError("4bit/8bit для NeMo-моделей (Canary/Parakeet) пока не добавлены.")
        try:
            from nemo.collections.asr.models import ASRModel
        except ModuleNotFoundError as e:
            raise RuntimeError('Не найден NeMo. Установите: pip install torch "nemo_toolkit[asr]>=2.2"') from e
        logger.info("NeMo ASR: loading %s", model_id)
        self._model = ASRModel.from_pretrained(model_name=model_id)
        self._model_id = model_id

    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        wav_path = _write_temp_wav_16k(audio_f32_16k)
        try:
            if self._model_id.startswith("nvidia/canary"):
                src = language if language and language in CANARY_SUPPORTED_LANGS else "en"
                output = self._model.transcribe(
                    [wav_path], source_lang=src, target_lang=src,
                )
                detected = src
            else:
                output = self._model.transcribe([wav_path])
                detected = language
            item = output[0]
            text = item.text if hasattr(item, "text") else str(item)
            return {"text": text.strip(), "language": detected, "seg_end_rel": None}
        finally:
            os.unlink(wav_path)


# ---------------------------------------------------------------------------
# Qwen3-ASR  (qwen-asr)
# ---------------------------------------------------------------------------

QWEN_LANG_MAP = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese",
    "ar": "Arabic", "de": "German", "fr": "French", "es": "Spanish",
    "pt": "Portuguese", "id": "Indonesian", "it": "Italian",
    "ko": "Korean", "ru": "Russian", "th": "Thai", "vi": "Vietnamese",
    "ja": "Japanese", "tr": "Turkish", "hi": "Hindi", "ms": "Malay",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "fi": "Finnish",
    "pl": "Polish", "cs": "Czech", "el": "Greek", "hu": "Hungarian",
    "ro": "Romanian",
}

QWEN_LANG_REVERSE = {v.lower(): k for k, v in QWEN_LANG_MAP.items()}


class QwenASRBackend(ASRBackend):

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        if importlib.util.find_spec("torch") is None:
            return 'Не найден torch. Установите: pip install torch "qwen-asr>=0.0.6"'
        if importlib.util.find_spec("qwen_asr") is None:
            return 'Не найден qwen-asr. Установите: pip install "qwen-asr>=0.0.6"'
        if quantization != "none":
            if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
                return "4bit/8bit для Qwen3-ASR сейчас поддержаны только на CUDA/Linux через bitsandbytes."
            if importlib.util.find_spec("bitsandbytes") is None:
                return "Для 4bit/8bit установите bitsandbytes: pip install bitsandbytes"
        return None

    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        try:
            import torch
        except ModuleNotFoundError as e:
            raise RuntimeError("Не найден torch. Установите: pip install torch 'qwen-asr>=0.0.6'") from e
        from qwen_asr import Qwen3ASRModel
        model_kwargs = _hf_model_kwargs(torch, quantization)

        if torch.cuda.is_available():
            device = "cuda:0"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        if quantization != "none" and not str(device).startswith("cuda"):
            raise RuntimeError("4bit/8bit для Qwen3-ASR сейчас поддержаны только на CUDA через bitsandbytes.")

        dtype = torch.bfloat16 if device != "cpu" else torch.float32
        logger.info("Qwen3-ASR: loading %s on %s", model_id, device)
        self._model = Qwen3ASRModel.from_pretrained(
            model_id,
            dtype=dtype,
            device_map=device,
            max_new_tokens=256,
            **model_kwargs,
        )

    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        lang_name = QWEN_LANG_MAP.get(language) if language else None
        results = self._model.transcribe(
            audio=(audio_f32_16k, TARGET_SAMPLE_RATE),
            language=lang_name,
        )
        r = results[0]
        detected_iso = language
        if hasattr(r, "language") and r.language:
            detected_iso = QWEN_LANG_REVERSE.get(
                r.language.lower(), r.language
            )
        return {
            "text": r.text.strip() if hasattr(r, "text") else str(r).strip(),
            "language": detected_iso,
            "seg_end_rel": None,
        }


class MacOSParakeetBackend(ASRBackend):
    """Fast macOS backend for Parakeet using FluidAudio CoreML CLI."""

    def preflight(self, model_id: str, quantization: str = "none") -> Optional[str]:
        if platform.system() != "Darwin":
            return "Parakeet CoreML fast path доступен только на macOS."
        if platform.machine() not in ("arm64",):
            return "Parakeet CoreML fast path рассчитан на Apple Silicon."
        if quantization != "none":
            return "Для Parakeet CoreML fast path выберите quantization=none."
        package_path = _fluidaudio_package_path()
        binary_path = _fluidaudio_binary_path(package_path)
        model_dir = _fluidaudio_model_dir()
        required = [
            model_dir / "Preprocessor.mlmodelc",
            model_dir / "Encoder.mlmodelc",
            model_dir / "Decoder.mlmodelc",
            model_dir / "JointDecision.mlmodelc",
            model_dir / "parakeet_vocab.json",
        ]
        if binary_path.exists() and all(p.exists() for p in required):
            return None
        if shutil.which("swift") is None:
            return "Не найден swift. Установите Xcode Command Line Tools / Swift toolchain."
        if not package_path.exists():
            return f"Не найден FluidAudio package по пути: {package_path}"
        if not all(p.exists() for p in required):
            free_bytes = shutil.disk_usage(str(Path.home())).free
            if free_bytes < 4_000_000_000:
                return "Для Parakeet CoreML нужно около 4+ GB свободного места."
        return None

    def load(self, model_id: str, n_threads: int, quantization: str = "none") -> None:
        package_path = _fluidaudio_package_path()
        binary_path = _fluidaudio_binary_path(package_path)
        if not binary_path.exists():
            logger.info("Building FluidAudio CLI at %s", package_path)
            proc = subprocess.run(
                [
                    "swift", "build",
                    "--package-path", str(package_path),
                    "--product", "fluidaudiocli",
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                raise RuntimeError(
                    "Не удалось собрать fluidaudiocli. "
                    + _compact_process_output(output)
                )
        self._binary_path = _fluidaudio_binary_path(package_path)
        if not self._binary_path.exists():
            raise RuntimeError(f"Не найден собранный fluidaudiocli: {self._binary_path}")

    def transcribe_once(
        self, audio_f32_16k: np.ndarray, language: Optional[str]
    ) -> dict:
        wav_path = _write_temp_wav_16k(audio_f32_16k)
        json_fd, json_path = tempfile.mkstemp(suffix=".json")
        os.close(json_fd)
        try:
            proc = subprocess.run(
                [
                    str(self._binary_path),
                    "transcribe",
                    wav_path,
                    "--output-json",
                    json_path,
                ],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                raise RuntimeError(
                    "FluidAudio transcribe failed. "
                    + _compact_process_output(output)
                )
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
            seg_end = payload.get("durationSeconds")
            word_timings = payload.get("wordTimings") or []
            if word_timings:
                try:
                    seg_end = max(float(w.get("endTime", 0.0)) for w in word_timings)
                except Exception:
                    pass
            return {
                "text": str(payload.get("text", "")).strip(),
                "language": language,
                "seg_end_rel": float(seg_end) if seg_end is not None else None,
            }
        finally:
            os.unlink(wav_path)
            if os.path.exists(json_path):
                os.unlink(json_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BACKEND_CLASSES: dict[str, type[ASRBackend]] = {
    "whisper_cpp": WhisperCppBackend,
    "voxtral": VoxtralBackend,
    "nemo_asr": NemoASRBackend,
    "macos_parakeet": MacOSParakeetBackend,
    "qwen_asr": QwenASRBackend,
}


def _hf_model_kwargs(torch_module, quantization: str) -> dict:
    if quantization not in QUANTIZATION_MODES:
        raise RuntimeError(f"Неизвестный режим quantization: {quantization}")
    if quantization == "none":
        return {}
    try:
        from transformers import BitsAndBytesConfig
    except ModuleNotFoundError as e:
        raise RuntimeError("Для 4bit/8bit нужен transformers с bitsandbytes: pip install bitsandbytes") from e
    except ImportError as e:
        raise RuntimeError("Для 4bit/8bit нужен transformers с BitsAndBytesConfig.") from e
    try:
        import bitsandbytes  # noqa: F401
    except ModuleNotFoundError as e:
        raise RuntimeError("Для 4bit/8bit установите bitsandbytes: pip install bitsandbytes") from e

    if quantization == "4bit":
        return {
            "quantization_config": BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch_module.bfloat16,
            )
        }
    return {
        "quantization_config": BitsAndBytesConfig(
            load_in_8bit=True,
        )
    }


def detect_backend_key(model_text: str) -> str:
    """Return the backend key for *model_text*."""
    if (
        model_text == "nvidia/parakeet-tdt-0.6b-v3"
        and platform.system() == "Darwin"
        and platform.machine() == "arm64"
    ):
        return "macos_parakeet"
    if model_text in KNOWN_MODELS:
        return KNOWN_MODELS[model_text]["backend"]
    return "whisper_cpp"


def create_backend(model_text: str) -> ASRBackend:
    """Instantiate (but don't load) the right backend for *model_text*."""
    key = detect_backend_key(model_text)
    cls = _BACKEND_CLASSES[key]
    return cls()


def preflight_backend(model_text: str, quantization: str = "none") -> tuple[bool, str]:
    backend = create_backend(model_text)
    error = backend.preflight(model_text, quantization=quantization)
    if error:
        return False, error
    return True, ""


def _fluidaudio_package_path() -> Path:
    override = os.environ.get("FLUIDAUDIO_PACKAGE_PATH")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "vendor" / "FluidAudio"


def _fluidaudio_binary_path(package_path: Path) -> Path:
    candidates = [
        package_path / ".build" / "debug" / "fluidaudiocli",
        package_path / ".build" / "arm64-apple-macosx" / "debug" / "fluidaudiocli",
        package_path / ".build" / "apple" / "Products" / "debug" / "fluidaudiocli",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _fluidaudio_model_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "FluidAudio" / "Models" / "parakeet-tdt-0.6b-v3-coreml"


def _compact_process_output(output: str, max_lines: int = 12) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "Нет подробного вывода."
    return " | ".join(lines[-max_lines:])
