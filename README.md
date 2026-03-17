# Dual Transcriber Web UI

Локальный веб-интерфейс для двухпоточной транскрибации (микрофон + системный звук/BlackHole) на `pywhispercpp`.

## Что умеет

- Запуск локального HTTP-сервера и открытие UI в браузере
- Простой вход по одному паролю без логина
- Два аудио-источника: `mic` и `remote`
- Запись `mic.jsonl`, `remote.jsonl`, `combined.jsonl`
- Склейка соседних фраз одного источника с разрывом по паузе
- Метрики в реальном времени:
  - RTF (`processing_time / audio_duration`)
  - оценка накопленной задержки
  - размер очереди
  - размер текущего буфера
  - количество фраз и слов
  - CPU / RAM процесса и системы
- Графики:
  - слова по времени
  - RTF по времени
  - накопленная задержка
  - накопление слов
- Масштаб графиков: `1 час` или `24 часа`
- Свободный ввод модели: имя встроенной модели **или** путь к локальному `.bin`

## Установка

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Запуск

```bash
python run.py
```

При первом запуске будет создан файл `config.json`.
Если пароля ещё нет, он сгенерируется автоматически и будет показан в консоли.

Пример консольного вывода:

```text
Server: http://127.0.0.1:8765
Password: 8P7kR2mQfL1x
```

## Настройка BlackHole

1. Установить BlackHole.
2. Создать `Multi-Output Device` в `Audio MIDI Setup`.
3. Добавить туда реальные колонки/наушники и `BlackHole 2ch`.
4. Выбрать `Multi-Output Device` как системный output.
5. В UI выбрать:
   - `mic device` = микрофон
   - `remote device` = `BlackHole 2ch`

## Модель

В поле `Модель` можно указывать:
- встроенное имя, например `large-v3-turbo-q5_0`
- или путь к локальному `.bin`, если файл реально существует

Если путь не существует, сервер не попробует грузить модель и вернёт понятную ошибку, чтобы не ловить падение `pywhispercpp`.

## SSH / удалённое хранилище

Прямую запись JSONL по SSH в этом проекте я не встраивал.
Практически надёжнее схема:

- писать локально на Mac
- синхронизировать после записи или по таймеру через `rsync`

Причина простая: если сеть на секунду отвалится, локальная запись не пострадает.

Хороший базовый вариант:

```bash
rsync -av ./transcripts/ user@remote-host:/path/to/archive/
```

Если захочешь, поверх этого проекта можно отдельно добавить кнопку `Sync via rsync`.

## Структура проекта

```text
transcriber_webui_project/
  run.py
  requirements.txt
  README.md
  app/
    __init__.py
    config.py
    controller.py
    transcriber.py
    templates/
      login.html
      index.html
    static/
      app.css
      app.js
```
