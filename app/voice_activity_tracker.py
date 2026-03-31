"""
Voice Activity Tracker - отслеживание срабатываний голосовых триггеров
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import threading


class VoiceActivityTracker:
    """Отслеживает когда и как часто срабатывали голосовые триггеры."""
    
    def __init__(self, output_file: Path):
        self.output_file = Path(output_file)
        self.lock = threading.Lock()
        self._ensure_file_exists()
    
    def _ensure_file_exists(self):
        """Создать файл если не существует."""
        if not self.output_file.exists():
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.output_file, 'w', encoding='utf-8') as f:
                json.dump([], f)
    
    def log_trigger(self, source: str, timestamp: Optional[datetime] = None):
        """
        Записать срабатывание триггера.
        
        Args:
            source: Источник звука ('mic' или 'remote')
            timestamp: Время срабатывания (если None - использовать текущее)
        """
        if timestamp is None:
            timestamp = datetime.now().astimezone()
        
        event = {
            'timestamp': timestamp.isoformat(timespec='seconds'),
            'source': source,
            'hour': timestamp.hour,
            'date': timestamp.date().isoformat(),
        }
        
        with self.lock:
            try:
                # Читаем существующие данные
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Добавляем новое событие
                data.append(event)
                
                # Оставляем только последние 30 дней
                cutoff_date = (datetime.now() - timedelta(days=30)).date().isoformat()
                data = [e for e in data if e.get('date', '') >= cutoff_date]
                
                # Записываем обратно
                with open(self.output_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
            
            except Exception as e:
                print(f"[VoiceActivityTracker] Error logging trigger: {e}")
    
    def get_hourly_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
        """
        Получить статистику по часам дня.
        
        Args:
            date_from: Начальная дата в формате ISO (если None - за последние 7 дней)
            date_to: Конечная дата в формате ISO (если None - сегодня)
        
        Returns:
            Dict с ключами:
                - hourly_mic: Dict[hour, count] - активность микрофона по часам
                - hourly_remote: Dict[hour, count] - активность системного звука по часам
                - total_mic: int - всего срабатываний микрофона
                - total_remote: int - всего срабатываний системного звука
                - date_range: [str, str] - фактический диапазон дат
        """
        with self.lock:
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = []
        
        # Определяем диапазон дат
        if date_to is None:
            date_to = datetime.now().date().isoformat()
        if date_from is None:
            from datetime import timedelta
            date_from = (datetime.now() - timedelta(days=7)).date().isoformat()
        
        # Фильтруем данные по диапазону
        filtered = [
            e for e in data
            if date_from <= e.get('date', '') <= date_to
        ]
        
        range_start = datetime.fromisoformat(f"{date_from}T00:00:00").astimezone()
        range_end = datetime.fromisoformat(f"{date_to}T23:59:59").astimezone()

        # Подсчитываем статистику
        hourly_mic = defaultdict(int)
        hourly_remote = defaultdict(int)
        total_mic = 0
        total_remote = 0
        series_buckets = defaultdict(lambda: {"mic": 0, "remote": 0})
        
        for event in filtered:
            source = event.get('source', '')
            hour = event.get('hour', 0)
            timestamp = event.get('timestamp')
            if timestamp:
                try:
                    event_dt = datetime.fromisoformat(timestamp)
                except ValueError:
                    event_dt = None
            else:
                event_dt = None
            
            if source == 'mic':
                hourly_mic[hour] += 1
                total_mic += 1
                if event_dt and range_start <= event_dt <= range_end:
                    bucket_dt = event_dt.replace(minute=0, second=0, microsecond=0)
                    series_buckets[bucket_dt.isoformat(timespec='seconds')]["mic"] += 1
            elif source == 'remote':
                hourly_remote[hour] += 1
                total_remote += 1
                if event_dt and range_start <= event_dt <= range_end:
                    bucket_dt = event_dt.replace(minute=0, second=0, microsecond=0)
                    series_buckets[bucket_dt.isoformat(timespec='seconds')]["remote"] += 1
        
        # Преобразуем в обычные dict для JSON
        series = []
        cursor = range_start.replace(minute=0, second=0, microsecond=0)
        while cursor <= range_end:
            bucket = series_buckets.get(cursor.isoformat(timespec='seconds'), {"mic": 0, "remote": 0})
            series.append({
                "ts": cursor.isoformat(timespec='seconds'),
                "mic": bucket["mic"],
                "remote": bucket["remote"],
            })
            cursor += timedelta(hours=1)

        return {
            'hourly_mic': dict(hourly_mic),
            'hourly_remote': dict(hourly_remote),
            'total_mic': total_mic,
            'total_remote': total_remote,
            'date_range': [date_from, date_to],
            'bucket_ms': 3600 * 1000,
            'series': series,
        }
    
    def get_daily_stats(self, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict:
        """
        Получить статистику по дням.
        
        Returns:
            Dict с ключами:
                - daily_mic: Dict[date, count]
                - daily_remote: Dict[date, count]
                - date_range: [str, str]
        """
        with self.lock:
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = []
        
        if date_to is None:
            date_to = datetime.now().date().isoformat()
        if date_from is None:
            from datetime import timedelta
            date_from = (datetime.now() - timedelta(days=30)).date().isoformat()
        
        filtered = [
            e for e in data
            if date_from <= e.get('date', '') <= date_to
        ]
        
        daily_mic = defaultdict(int)
        daily_remote = defaultdict(int)
        series_buckets = defaultdict(lambda: {"mic": 0, "remote": 0})
        
        for event in filtered:
            source = event.get('source', '')
            date = event.get('date', '')
            
            if source == 'mic':
                daily_mic[date] += 1
                series_buckets[date]["mic"] += 1
            elif source == 'remote':
                daily_remote[date] += 1
                series_buckets[date]["remote"] += 1

        series = []
        cursor = datetime.fromisoformat(f"{date_from}T00:00:00").date()
        end_date = datetime.fromisoformat(f"{date_to}T00:00:00").date()
        local_tz = datetime.now().astimezone().tzinfo
        while cursor <= end_date:
            bucket = series_buckets.get(cursor.isoformat(), {"mic": 0, "remote": 0})
            series.append({
                "ts": datetime.combine(cursor, datetime.min.time(), tzinfo=local_tz).isoformat(timespec='seconds'),
                "mic": bucket["mic"],
                "remote": bucket["remote"],
            })
            cursor += timedelta(days=1)
        
        return {
            'daily_mic': dict(daily_mic),
            'daily_remote': dict(daily_remote),
            'date_range': [date_from, date_to],
            'bucket_ms': 24 * 3600 * 1000,
            'series': series,
        }


# Глобальный трекер
_tracker: Optional[VoiceActivityTracker] = None


def init_tracker(output_dir: Path):
    """Инициализировать глобальный трекер."""
    global _tracker
    output_file = output_dir / 'voice_activity.jsonl'
    _tracker = VoiceActivityTracker(output_file)
    return _tracker


def log_voice_trigger(source: str, timestamp: Optional[datetime] = None):
    """Записать срабатывание голосового триггера."""
    if _tracker:
        _tracker.log_trigger(source, timestamp)


def get_tracker() -> Optional[VoiceActivityTracker]:
    """Получить текущий трекер."""
    return _tracker
