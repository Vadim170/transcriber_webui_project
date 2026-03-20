#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VENV = ROOT / ".venv"
FLUIDAUDIO_REPO = "https://github.com/FluidInference/FluidAudio.git"
FLUIDAUDIO_DIR = ROOT / "vendor" / "FluidAudio"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    printable = " ".join(cmd)
    print(f"\n>>> {printable}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)


def detect_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in ("python3.11", "python3", "python"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("Не найден Python 3. Установите python3.11+ и повторите.")


def ensure_venv(python_bin: str, venv_path: Path) -> Path:
    if not venv_path.exists():
        run([python_bin, "-m", "venv", str(venv_path)])
    pip_path = venv_path / "bin" / "pip"
    if not pip_path.exists():
        raise RuntimeError(f"Не найден pip внутри venv: {pip_path}")
    return pip_path


def ensure_core_requirements(pip_path: Path) -> None:
    run([str(pip_path), "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([str(pip_path), "install", "-r", str(ROOT / "requirements.txt")])


def install_optional_backends(pip_path: Path, args: argparse.Namespace) -> None:
    if args.with_voxtral or args.all_backends:
        run([str(pip_path), "install", "torch", "transformers", "accelerate"])
    if args.with_nemo or args.all_backends:
        run([str(pip_path), "install", "torch", "nemo_toolkit[asr]>=2.2"])
    if args.with_qwen or args.all_backends:
        run([str(pip_path), "install", "torch", "qwen-asr>=0.0.6"])


def ensure_swift_available() -> None:
    if shutil.which("swift") is None:
        raise RuntimeError(
            "Не найден swift. Установите Xcode Command Line Tools: xcode-select --install"
        )


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("Не найден git. Установите git и повторите.")


def ensure_fluidaudio_repo(update: bool) -> None:
    ensure_git_available()
    FLUIDAUDIO_DIR.parent.mkdir(parents=True, exist_ok=True)
    if FLUIDAUDIO_DIR.exists():
        if update:
            run(["git", "-C", str(FLUIDAUDIO_DIR), "pull", "--ff-only"])
        else:
            print(f"FluidAudio уже существует: {FLUIDAUDIO_DIR}")
        return
    run(["git", "clone", FLUIDAUDIO_REPO, str(FLUIDAUDIO_DIR)])


def build_fluidaudio() -> None:
    ensure_swift_available()
    if platform.system() != "Darwin":
        print("Внимание: FluidAudio helper в первую очередь рассчитан на macOS.")
    run(["swift", "build", "--package-path", str(FLUIDAUDIO_DIR), "--product", "fluidaudiocli"])


def print_summary(venv_path: Path, args: argparse.Namespace) -> None:
    python_path = venv_path / "bin" / "python"
    print("\nSetup завершён.\n")
    print("Следующие шаги:")
    print(f"1. Активировать окружение: source {venv_path}/bin/activate")
    print("2. Запустить сервер: python run.py")
    print("3. Открыть URL и войти по паролю из консоли.")
    print("\nЕсли нужен FluidAudio CoreML:")
    if args.with_fluidaudio:
        print(f"- Swift package: {FLUIDAUDIO_DIR}")
        print("- При первом использовании модели CoreML может начаться загрузка/подготовка моделей.")
    else:
        print("- Перезапустите helper с флагом --with-fluidaudio")
    print(f"\nPython в venv: {python_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Setup helper for Dual Transcriber Web UI on macOS."
    )
    parser.add_argument("--python", help="Python interpreter to use, e.g. python3.11")
    parser.add_argument("--venv", default=str(DEFAULT_VENV), help="Virtualenv path")
    parser.add_argument("--with-voxtral", action="store_true", help="Install Voxtral dependencies")
    parser.add_argument("--with-nemo", action="store_true", help="Install NVIDIA Canary/Parakeet dependencies")
    parser.add_argument("--with-qwen", action="store_true", help="Install Qwen3-ASR dependencies")
    parser.add_argument("--all-backends", action="store_true", help="Install dependencies for all optional Python backends")
    parser.add_argument("--with-fluidaudio", action="store_true", help="Clone/build vendor/FluidAudio and fluidaudiocli")
    parser.add_argument("--update-fluidaudio", action="store_true", help="If FluidAudio already exists, pull latest changes before build")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.chdir(ROOT)
    print(f"Project root: {ROOT}")
    python_bin = detect_python(args.python)
    venv_path = Path(args.venv).expanduser().resolve()
    pip_path = ensure_venv(python_bin, venv_path)
    ensure_core_requirements(pip_path)
    install_optional_backends(pip_path, args)
    if args.with_fluidaudio:
        ensure_fluidaudio_repo(args.update_fluidaudio)
        build_fluidaudio()
    print_summary(venv_path, args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\nОшибка: команда завершилась с кодом {exc.returncode}")
        raise SystemExit(exc.returncode)
    except Exception as exc:
        print(f"\nОшибка: {exc}")
        raise SystemExit(1)
