# Dual Transcriber Web UI

Локальная веб-панель для двухпоточной транскрибации:

- `mic` = микрофон
- `remote` = системный звук / BlackHole

Проект умеет не только live-транскрибацию, но и хранение полного аудио, скачивание интервалов по времени и повторную ретранскрибацию выбранного диапазона.

## Что умеет

- локальный HTTP-сервер с UI и простым входом по одному паролю
- два независимых аудио-источника: `mic` и `remote`
- live-транскрибация в `mic.jsonl`, `remote.jsonl`, `combined.jsonl`
- архив полного аудио по дням:
  - отдельный файл для `mic`
  - отдельный файл для `remote`
  - настраиваемое удержание архива в сутках
- history UI:
  - просмотр combined-фраз по диапазону
  - обзор активности за 24 часа
  - обзор покрытия полного аудио за 24 часа
  - скачивание аудиоклипа по времени отдельно для `mic` и `remote`
  - повторная транскрибация выбранного интервала с прогрессом
- метрики в реальном времени:
  - RTF
  - накопленная задержка
  - очередь и буфер
  - количество слов и фраз
  - CPU / RAM процесса и системы
- несколько backend'ов ASR:
  - `whisper.cpp`
  - `Voxtral`
  - `NVIDIA Canary / Parakeet`
  - `Qwen3-ASR`
  - `FluidAudio CoreML` для macOS / Apple Silicon

## Требования

Базовые:

- macOS или Linux
- Python `3.11+`
- микрофон / устройство loopback для системного аудио

Для macOS fast path (`FluidAudio CoreML`) дополнительно:

- Apple Silicon
- `swift`
- локальный Swift package `vendor/FluidAudio` или `FLUIDAUDIO_PACKAGE_PATH`

## Быстрый старт

Самый удобный вариант на macOS:

```bash
python3 scripts/setup_macos.py
source .venv/bin/activate
python run.py
```

Если нужен `FluidAudio CoreML`:

```bash
python3 scripts/setup_macos.py --with-fluidaudio
source .venv/bin/activate
python run.py
```

## Setup Helper

В проект добавлен helper:

```bash
python3 scripts/setup_macos.py --help
```

Он умеет:

- создать `.venv`
- обновить `pip`, `setuptools`, `wheel`
- установить `requirements.txt`
- установить зависимости для опциональных backend'ов
- скачать `vendor/FluidAudio`
- собрать `fluidaudiocli`

Примеры:

```bash
# Только базовая установка
python3 scripts/setup_macos.py

# База + FluidAudio
python3 scripts/setup_macos.py --with-fluidaudio

# База + Voxtral + Qwen
python3 scripts/setup_macos.py --with-voxtral --with-qwen

# Всё сразу
python3 scripts/setup_macos.py --all-backends --with-fluidaudio
```

## Ручная установка

Если хочешь всё сделать сам:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

## Опциональные backend'ы

Базовый backend уже входит в `requirements.txt`:

- `whisper.cpp` через `pywhispercpp`

Дополнительно можно установить:

### Voxtral

```bash
pip install torch transformers accelerate
```

### NVIDIA Canary / Parakeet

```bash
pip install torch "nemo_toolkit[asr]>=2.2"
```

### Qwen3-ASR

```bash
pip install torch "qwen-asr>=0.0.6"
```

### 4-bit / 8-bit для Voxtral / Qwen на CUDA

```bash
pip install bitsandbytes
```

### FluidAudio CoreML (macOS / Apple Silicon)

```bash
xcode-select --install
git clone https://github.com/FluidInference/FluidAudio.git vendor/FluidAudio
swift build --package-path vendor/FluidAudio --product fluidaudiocli
```

Проект ожидает:

- Swift package по пути `vendor/FluidAudio`
- либо путь из `FLUIDAUDIO_PACKAGE_PATH`

CLI бинарник ищется в одном из путей внутри `.build`.

## Запуск

```bash
source .venv/bin/activate
python run.py
```

При первом запуске будет создан `config.json`.
Если пароль ещё не задан, он сгенерируется автоматически и будет показан в консоли.

Пример:

```text
Server: http://127.0.0.1:8765
Password: 8P7kR2mQfL1x
```

> **Note:** Пароль хранится в открытом виде в `config.json`. Если старый `config.json` содержал хэш (строка начинается с `scrypt:` или `pbkdf2:`), удали файл и перезапусти сервер — пароль сгенерируется заново.

## Production deployment

Встроенный Flask-сервер предназначен **только для локального использования**.
Для запуска в сети или продакшене используй `gunicorn`:

```bash
pip install gunicorn
gunicorn -w 1 -b 127.0.0.1:8765 "run:app"
```

> **Important:** Только `-w 1` (один воркер). `TranscriberController` хранит состояние в памяти процесса — несколько воркеров создадут изолированные независимые контроллеры.

## Настройка BlackHole на macOS

1. Установить `BlackHole`.
2. Открыть `Audio MIDI Setup`.
3. Создать `Multi-Output Device`.
4. Добавить в него:
   - реальные колонки / наушники
   - `BlackHole 2ch`
5. Выбрать `Multi-Output Device` как системный output.
6. В UI выбрать:
   - `Микрофон` = твой реальный микрофон
   - `Системный звук / BlackHole` = `BlackHole 2ch`

## Настройки в UI

На главной странице доступны:

- устройство `mic`
- устройство `remote`
- модель
- количество потоков
- quantization
- папка логов
- включение архива полного аудио
- количество суток хранения архива
- папка архива аудио

## Полное аудио

Архив полного аудио хранится отдельно от транскрипции.

По умолчанию:

- архив включён
- папка архива: `./audio_archive`
- retention: `1` сутки

Логика:

- для каждого дня создаются отдельные файлы
- отдельно для `mic`
- отдельно для `remote`
- старые дни удаляются по retention window

## History / Повторная обработка

Страница `/history` умеет:

- загрузить combined-фразы за диапазон
- показать, за какие интервалы есть полная аудиозапись
- скачать WAV-клип по диапазону отдельно для `mic` и `remote`
- повторно транскрибировать выбранный интервал как полное аудио обеих дорожек

Повторная транскрибация:

- не использует уже записанный `combined.jsonl`
- заново прогоняет выбранный интервал через модель
- показывает прогресс в UI
- возвращает временный результат
- позволяет скачать результат как `JSON` или `TXT`

## Модель

В поле `Модель` можно указывать:

- встроенное имя модели
- путь к локальному `.bin`, если файл существует

Если путь не существует, backend не будет загружен и сервер вернёт понятную ошибку.

## Полезные пути

- конфиг: `config.json`
- текстовые результаты: `./transcripts`
- архив полного аудио: `./audio_archive`
- `FluidAudio` модели:
  - `~/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3-coreml`

## Troubleshooting

### `Не найден pywhispercpp`

Установи зависимости проекта:

```bash
pip install -r requirements.txt
```

### `Не найден swift`

Установи Command Line Tools:

```bash
xcode-select --install
```

### `Не найден FluidAudio package`

Скачай пакет:

```bash
git clone https://github.com/FluidInference/FluidAudio.git vendor/FluidAudio
```

### Не видно системный звук

Проверь:

- установлен ли `BlackHole`
- создан ли `Multi-Output Device`
- выбран ли правильный `remote device` в UI

### Не работает ретранскрибация

Сначала проверь:

- существует ли полное аудио за этот диапазон
- выбрана ли рабочая модель
- установлены ли зависимости именно для выбранного backend'а

## SSH / удалённое хранилище

Прямую запись JSONL по SSH проект не делает.

Практический вариант:

- писать локально на Mac
- синхронизировать потом через `rsync`

Пример:

```bash
rsync -av ./transcripts/ user@remote-host:/path/to/archive/
```

## Структура проекта

```text
transcriber_webui_project/
  run.py
  requirements.txt
  README.md
  scripts/
    setup_macos.py
  app/
    __init__.py
    backends.py
    config.py
    controller.py
    model_manager.py
    transcriber.py
    templates/
      history.html
      index.html
      login.html
    static/
      app.css
      app.js
      history.js
```
