# API Documentation

## Endpoints

### Authentication

All API endpoints except `/api/login` require authentication via session cookie.

#### POST /api/login

Authenticate and create a session.

**Request:**
```json
{
  "password": "your_password"
}
```

**Response (Success):**
```json
{
  "ok": true
}
```

**Response (Error):**
```json
{
  "ok": false,
  "error": "Неверный пароль."
}
```

---

### Devices

#### GET /api/devices

Get list of available audio input devices.

**Response:**
```json
{
  "ok": true,
  "devices": [
    {
      "id": 0,
      "name": "Built-in Microphone",
      "channels": 1,
      "sample_rate": 48000
    }
  ]
}
```

---

### Models

#### GET /api/models/status

Get status of available ASR models and backends.

**Response:**
```json
{
  "ok": true,
  "groups": [
    {
      "name": "Whisper.cpp",
      "models": [
        {
          "id": "base",
          "name": "Base",
          "available": true,
          "error": null
        }
      ]
    }
  ]
}
```

#### POST /api/models/preload

Preload a model into memory.

**Request:**
```json
{
  "model": "base",
  "quantization": "none"
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Model loaded successfully"
}
```

#### POST /api/models/delete

Unload a model from memory.

**Request:**
```json
{
  "model": "base"
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Model deleted"
}
```

---

### Configuration

#### GET /api/config

Get current configuration.

**Response:**
```json
{
  "ok": true,
  "config": {
    "host": "127.0.0.1",
    "port": 8765,
    "mic_device": 0,
    "remote_device": 1,
    "model": "base",
    "quantization": "none",
    "threads": 4,
    "out_dir": "./transcripts",
    "language": "auto",
    "min_interval_s": 300,
    "max_interval_s": 600,
    "silence_cut_ms": 2000,
    "audio_queue_size": 2048
  }
}
```

#### POST /api/config

Update configuration.

**Request:**
```json
{
  "mic_device": 0,
  "remote_device": 1,
  "model": "base",
  "threads": 4
}
```

**Response:**
```json
{
  "ok": true
}
```

---

### Transcription Control

#### POST /api/start

Start live transcription.

**Request:**
```json
{
  "mic_device": 0,
  "remote_device": 1,
  "model": "base",
  "quantization": "none",
  "threads": 4,
  "out_dir": "./transcripts"
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Started successfully"
}
```

#### POST /api/stop

Stop live transcription.

**Response:**
```json
{
  "ok": true,
  "message": "Stopped successfully"
}
```

#### GET /api/state

Get current transcription state and metrics.

**Response:**
```json
{
  "ok": true,
  "state": {
    "running": true,
    "loading": false,
    "model_loaded": true,
    "model_name": "base",
    "started_at": "2026-03-20T12:00:00+03:00",
    "sources": {
      "mic": {
        "role": "mic",
        "enabled": true,
        "device_id": 0,
        "device_name": "Built-in Microphone",
        "status": "running",
        "queue_size": 10,
        "last_rtf": 0.25,
        "lag_estimate_sec": 1.2,
        "words": 150
      },
      "remote": {
        "role": "remote",
        "enabled": true,
        "device_id": 1,
        "device_name": "BlackHole 2ch",
        "status": "running",
        "queue_size": 8,
        "last_rtf": 0.22,
        "lag_estimate_sec": 0.9,
        "words": 120
      }
    },
    "current_interval": {
      "start_at": "2026-03-20T12:00:00+03:00",
      "elapsed_s": 180.5
    }
  }
}
```

---

### Intervals

#### GET /api/intervals

Get all intervals that overlap with the specified time range.

**Query Parameters:**
- `from` (required): ISO 8601 datetime string (start of range)
- `to` (required): ISO 8601 datetime string (end of range)

**Example:**
```
GET /api/intervals?from=2026-03-20T10:00:00Z&to=2026-03-20T12:00:00Z
```

**Response:**
```json
{
  "ok": true,
  "from": "2026-03-20T10:00:00+00:00",
  "to": "2026-03-20T12:00:00+00:00",
  "count": 2,
  "intervals": [
    {
      "type": "interval",
      "start_at": "2026-03-20T09:50:00+03:00",
      "end_at": "2026-03-20T10:15:00+03:00",
      "duration_s": 1500.0,
      "mic_text": "Transcribed text from microphone...",
      "remote_text": "Transcribed text from remote audio...",
      "mic_language": "ru",
      "remote_language": "ru"
    }
  ]
}
```

**Notes:**
- Returns intervals that have ANY overlap with the time range
- Full interval data is returned even if only partially overlapping
- Intervals are from `intervals.jsonl`

#### GET /api/intervals/overview

Get lightweight overview of all intervals (without full text).

**Response:**
```json
{
  "ok": true,
  "intervals": [
    {
      "start_at": "2026-03-20T09:00:00+03:00",
      "end_at": "2026-03-20T09:10:00+03:00",
      "duration_s": 600.0
    }
  ],
  "current": {
    "start_at": "2026-03-20T12:00:00+03:00",
    "elapsed_s": 180.5
  }
}
```

---

### Transcriptions

#### GET /api/transcriptions

Get individual utterances that fall completely within the specified time range.

**Query Parameters:**
- `from` (required): ISO 8601 datetime string (start of range)
- `to` (required): ISO 8601 datetime string (end of range)

**Example:**
```
GET /api/transcriptions?from=2026-03-20T10:00:00Z&to=2026-03-20T12:00:00Z
```

**Response:**
```json
{
  "ok": true,
  "from": "2026-03-20T10:00:00+00:00",
  "to": "2026-03-20T12:00:00+00:00",
  "count": 150,
  "utterances": [
    {
      "type": "utterance",
      "role": "mic",
      "text": "Привет, как дела?",
      "start_s": 3.4,
      "end_s": 5.2,
      "start_at": "2026-03-20T10:00:03+03:00",
      "end_at": "2026-03-20T10:00:05+03:00",
      "language": "ru"
    },
    {
      "type": "utterance",
      "role": "remote",
      "text": "Hello, how are you?",
      "start_s": 10.1,
      "end_s": 12.5,
      "start_at": "2026-03-20T10:00:10+03:00",
      "end_at": "2026-03-20T10:00:12+03:00",
      "language": "en"
    }
  ]
}
```

**Notes:**
- Returns only utterances that are **completely** within the time range
- Excludes utterances that start before `from` or end after `to`
- Utterances are from `combined.jsonl`
- Each utterance includes:
  - `role`: "mic" or "remote" (audio source)
  - `text`: transcribed text
  - `start_s`, `end_s`: relative timestamps within interval
  - `start_at`, `end_at`: absolute ISO 8601 timestamps
  - `language`: detected language code

**Difference from `/api/intervals`:**
- `/api/intervals` returns large time blocks with aggregated text (any overlap)
- `/api/transcriptions` returns individual phrases/sentences (complete overlap only)
- Use `/api/transcriptions` for precise time-based queries
- Use `/api/intervals` for browsing recording sessions

---

## Data Formats

### Utterance Object

```json
{
  "type": "utterance",
  "role": "mic",
  "text": "Transcribed text",
  "start_s": 3.4,
  "end_s": 5.2,
  "start_at": "2026-03-20T10:00:03+03:00",
  "end_at": "2026-03-20T10:00:05+03:00",
  "language": "ru"
}
```

### Interval Object

```json
{
  "type": "interval",
  "start_at": "2026-03-20T09:00:00+03:00",
  "end_at": "2026-03-20T09:10:00+03:00",
  "duration_s": 600.0,
  "mic_text": "Full transcription from microphone during this interval...",
  "remote_text": "Full transcription from remote audio during this interval...",
  "mic_language": "ru",
  "remote_language": "en"
}
```

### Timestamp Format

All timestamps use ISO 8601 format with timezone:
- `2026-03-20T10:00:00+03:00` (with timezone offset)
- `2026-03-20T10:00:00Z` (UTC)

---

## Error Responses

All endpoints return errors in this format:

```json
{
  "ok": false,
  "error": "Error message in Russian or English"
}
```

Common HTTP status codes:
- `200` - Success
- `400` - Bad request (invalid parameters)
- `401` - Unauthorized (not logged in)
- `500` - Internal server error
