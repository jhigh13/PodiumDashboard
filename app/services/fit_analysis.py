"""
FIT File Analysis Service

This module provides comprehensive analysis of FIT files downloaded from TrainingPeaks.
Uses fitdecode library to parse FIT files and extract workout metrics including:
- Lap data (times, distances, avg/max heart rate, power, pace)
- Record data (time-series: HR, power, cadence, speed, position)
- Session summary (totals, averages, maximums)
- Device information
"""

import fitdecode
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LapMetrics:
    """Metrics for a single lap"""
    lap_number: int
    start_time: Optional[datetime] = None
    total_elapsed_time: Optional[float] = None  # seconds
    total_timer_time: Optional[float] = None  # seconds (active time)
    total_distance: Optional[float] = None  # meters
    avg_speed: Optional[float] = None  # m/s
    max_speed: Optional[float] = None  # m/s
    avg_heart_rate: Optional[int] = None  # bpm
    max_heart_rate: Optional[int] = None  # bpm
    avg_power: Optional[int] = None  # watts
    max_power: Optional[int] = None  # watts
    avg_cadence: Optional[int] = None  # rpm or spm
    max_cadence: Optional[int] = None  # rpm or spm
    total_calories: Optional[int] = None
    avg_temperature: Optional[int] = None  # celsius
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, formatting times as strings"""
        result = {}
        for key, value in self.__dict__.items():
            if key == 'start_time' and value:
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result


@dataclass
class RecordPoint:
    """Single time-series data point from workout"""
    timestamp: Optional[datetime] = None
    position_lat: Optional[float] = None  # semicircles
    position_long: Optional[float] = None  # semicircles
    distance: Optional[float] = None  # meters
    altitude: Optional[float] = None  # meters
    speed: Optional[float] = None  # m/s
    heart_rate: Optional[int] = None  # bpm
    cadence: Optional[int] = None  # rpm or spm
    power: Optional[int] = None  # watts
    temperature: Optional[int] = None  # celsius
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, formatting timestamp as string"""
        result = {}
        for key, value in self.__dict__.items():
            if key == 'timestamp' and value:
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result


@dataclass
class SessionSummary:
    """Overall workout session summary"""
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    start_time: Optional[datetime] = None
    total_elapsed_time: Optional[float] = None  # seconds
    total_timer_time: Optional[float] = None  # seconds (active time)
    total_distance: Optional[float] = None  # meters
    total_calories: Optional[int] = None
    avg_speed: Optional[float] = None  # m/s
    max_speed: Optional[float] = None  # m/s
    avg_heart_rate: Optional[int] = None  # bpm
    max_heart_rate: Optional[int] = None  # bpm
    avg_power: Optional[int] = None  # watts
    max_power: Optional[int] = None  # watts
    normalized_power: Optional[int] = None  # watts (NP)
    avg_cadence: Optional[int] = None  # rpm or spm
    max_cadence: Optional[int] = None  # rpm or spm
    total_ascent: Optional[float] = None  # meters
    total_descent: Optional[float] = None  # meters
    training_stress_score: Optional[float] = None  # TSS
    intensity_factor: Optional[float] = None  # IF
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, formatting times as strings"""
        result = {}
        for key, value in self.__dict__.items():
            if key == 'start_time' and value:
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result


@dataclass
class DeviceInfo:
    """Information about the device that recorded the workout"""
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    serial_number: Optional[int] = None
    software_version: Optional[str] = None
    device_index: Optional[int] = None


@dataclass
class WorkoutAnalysis:
    """Complete analysis of a FIT file workout"""
    file_path: str
    session: SessionSummary = field(default_factory=SessionSummary)
    laps: List[LapMetrics] = field(default_factory=list)
    records: List[RecordPoint] = field(default_factory=list)
    devices: List[DeviceInfo] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert entire analysis to dictionary"""
        return {
            'file_path': self.file_path,
            'session': self.session.to_dict(),
            'laps': [lap.to_dict() for lap in self.laps],
            'records': [rec.to_dict() for rec in self.records],
            'devices': [dev.__dict__ for dev in self.devices],
            'summary': self.get_summary()
        }
    
    def get_summary(self) -> Dict[str, Any]:
        """Get high-level summary statistics"""
        return {
            'sport': self.session.sport,
            'duration_seconds': self.session.total_timer_time,
            'distance_meters': self.session.total_distance,
            'distance_km': round(self.session.total_distance / 1000, 2) if self.session.total_distance else None,
            'distance_miles': round(self.session.total_distance / 1609.34, 2) if self.session.total_distance else None,
            'avg_pace_per_km': self._format_pace(self.session.avg_speed, 1000) if self.session.avg_speed else None,
            'avg_pace_per_mile': self._format_pace(self.session.avg_speed, 1609.34) if self.session.avg_speed else None,
            'avg_heart_rate': self.session.avg_heart_rate,
            'max_heart_rate': self.session.max_heart_rate,
            'avg_power': self.session.avg_power,
            'max_power': self.session.max_power,
            'total_calories': self.session.total_calories,
            'lap_count': len(self.laps),
            'record_count': len(self.records),
        }
    
    @staticmethod
    def _format_pace(speed_ms: float, distance_unit: float) -> str:
        """Convert speed (m/s) to pace string (min:sec per km/mile)"""
        if speed_ms <= 0:
            return "0:00"
        seconds_per_unit = distance_unit / speed_ms
        minutes = int(seconds_per_unit // 60)
        seconds = int(seconds_per_unit % 60)
        return f"{minutes}:{seconds:02d}"


class FitFileAnalyzer:
    """Analyzes FIT files and extracts comprehensive workout data"""
    
    def __init__(self, fit_file_path: str):
        """
        Initialize analyzer with path to FIT file
        
        Args:
            fit_file_path: Path to the .fit file to analyze
        """
        self.fit_file_path = fit_file_path
        if not Path(fit_file_path).exists():
            raise FileNotFoundError(f"FIT file not found: {fit_file_path}")
    
    def analyze(self, include_records: bool = True) -> WorkoutAnalysis:
        """
        Analyze the FIT file and extract all metrics
        
        Args:
            include_records: Whether to include time-series records (can be large)
        
        Returns:
            WorkoutAnalysis object containing all extracted data
        """
        analysis = WorkoutAnalysis(file_path=self.fit_file_path)
        lap_count = 0
        
        with fitdecode.FitReader(self.fit_file_path) as fit:
            for frame in fit:
                if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                    # Process different message types
                    if frame.name == 'session':
                        analysis.session = self._extract_session(frame)
                    elif frame.name == 'lap':
                        lap_count += 1
                        lap = self._extract_lap(frame, lap_count)
                        analysis.laps.append(lap)
                    elif frame.name == 'record' and include_records:
                        record = self._extract_record(frame)
                        analysis.records.append(record)
                    elif frame.name == 'device_info':
                        device = self._extract_device_info(frame)
                        analysis.devices.append(device)
        
        return analysis
    
    def _extract_session(self, frame: fitdecode.FitDataMessage) -> SessionSummary:
        """Extract session summary data"""
        session = SessionSummary()
        
        for field in frame.fields:
            field_name = field.name
            value = field.value
            
            if field_name == 'sport':
                session.sport = str(value)
            elif field_name == 'sub_sport':
                session.sub_sport = str(value)
            elif field_name == 'start_time':
                session.start_time = value
            elif field_name == 'total_elapsed_time':
                session.total_elapsed_time = value
            elif field_name == 'total_timer_time':
                session.total_timer_time = value
            elif field_name == 'total_distance':
                session.total_distance = value
            elif field_name == 'total_calories':
                session.total_calories = value
            elif field_name == 'avg_speed':
                session.avg_speed = value
            elif field_name == 'max_speed':
                session.max_speed = value
            elif field_name == 'avg_heart_rate':
                session.avg_heart_rate = value
            elif field_name == 'max_heart_rate':
                session.max_heart_rate = value
            elif field_name == 'avg_power':
                session.avg_power = value
            elif field_name == 'max_power':
                session.max_power = value
            elif field_name == 'normalized_power':
                session.normalized_power = value
            elif field_name == 'avg_cadence':
                session.avg_cadence = value
            elif field_name == 'max_cadence':
                session.max_cadence = value
            elif field_name == 'total_ascent':
                session.total_ascent = value
            elif field_name == 'total_descent':
                session.total_descent = value
            elif field_name == 'training_stress_score':
                session.training_stress_score = value
            elif field_name == 'intensity_factor':
                session.intensity_factor = value
        
        return session
    
    def _extract_lap(self, frame: fitdecode.FitDataMessage, lap_number: int) -> LapMetrics:
        """Extract lap metrics"""
        lap = LapMetrics(lap_number=lap_number)
        
        for field in frame.fields:
            field_name = field.name
            value = field.value
            
            if field_name == 'start_time':
                lap.start_time = value
            elif field_name == 'total_elapsed_time':
                lap.total_elapsed_time = value
            elif field_name == 'total_timer_time':
                lap.total_timer_time = value
            elif field_name == 'total_distance':
                lap.total_distance = value
            elif field_name == 'avg_speed':
                lap.avg_speed = value
            elif field_name == 'max_speed':
                lap.max_speed = value
            elif field_name == 'avg_heart_rate':
                lap.avg_heart_rate = value
            elif field_name == 'max_heart_rate':
                lap.max_heart_rate = value
            elif field_name == 'avg_power':
                lap.avg_power = value
            elif field_name == 'max_power':
                lap.max_power = value
            elif field_name == 'avg_cadence':
                lap.avg_cadence = value
            elif field_name == 'max_cadence':
                lap.max_cadence = value
            elif field_name == 'total_calories':
                lap.total_calories = value
            elif field_name == 'avg_temperature':
                lap.avg_temperature = value
        
        return lap
    
    def _extract_record(self, frame: fitdecode.FitDataMessage) -> RecordPoint:
        """Extract time-series record point"""
        record = RecordPoint()
        
        for field in frame.fields:
            field_name = field.name
            value = field.value
            
            if field_name == 'timestamp':
                record.timestamp = value
            elif field_name == 'position_lat':
                record.position_lat = value
            elif field_name == 'position_long':
                record.position_long = value
            elif field_name == 'distance':
                record.distance = value
            elif field_name == 'altitude':
                record.altitude = value
            elif field_name == 'speed':
                record.speed = value
            elif field_name == 'heart_rate':
                record.heart_rate = value
            elif field_name == 'cadence':
                record.cadence = value
            elif field_name == 'power':
                record.power = value
            elif field_name == 'temperature':
                record.temperature = value
        
        return record
    
    def _extract_device_info(self, frame: fitdecode.FitDataMessage) -> DeviceInfo:
        """Extract device information"""
        device = DeviceInfo()
        
        for field in frame.fields:
            field_name = field.name
            value = field.value
            
            if field_name == 'manufacturer':
                device.manufacturer = str(value)
            elif field_name == 'product':
                device.product = str(value)
            elif field_name == 'serial_number':
                device.serial_number = value
            elif field_name == 'software_version':
                device.software_version = str(value) if value else None
            elif field_name == 'device_index':
                device.device_index = value
        
        return device
    
    def get_quick_summary(self) -> Dict[str, Any]:
        """
        Get a quick summary without full analysis (faster for large files)
        Only extracts session and lap data, skips time-series records
        """
        analysis = self.analyze(include_records=False)
        return analysis.get_summary()
    
    def extract_hr_zones(self, max_hr: int = 190) -> Dict[str, Any]:
        """
        Calculate time spent in heart rate zones
        
        Args:
            max_hr: Maximum heart rate for zone calculation (default 190)
        
        Returns:
            Dictionary with time in each zone
        """
        zones = {
            'Zone 1 (50-60%)': 0,  # Recovery
            'Zone 2 (60-70%)': 0,  # Aerobic
            'Zone 3 (70-80%)': 0,  # Tempo
            'Zone 4 (80-90%)': 0,  # Threshold
            'Zone 5 (90-100%)': 0,  # VO2 Max
        }
        
        analysis = self.analyze(include_records=True)
        
        for i, record in enumerate(analysis.records):
            if record.heart_rate:
                hr_percent = (record.heart_rate / max_hr) * 100
                
                # Calculate time duration (seconds between records)
                if i > 0 and analysis.records[i-1].timestamp and record.timestamp:
                    duration = (record.timestamp - analysis.records[i-1].timestamp).total_seconds()
                else:
                    duration = 1  # Default 1 second if timestamp missing
                
                # Classify into zone
                if hr_percent < 60:
                    zones['Zone 1 (50-60%)'] += duration
                elif hr_percent < 70:
                    zones['Zone 2 (60-70%)'] += duration
                elif hr_percent < 80:
                    zones['Zone 3 (70-80%)'] += duration
                elif hr_percent < 90:
                    zones['Zone 4 (80-90%)'] += duration
                else:
                    zones['Zone 5 (90-100%)'] += duration
        
        # Convert to minutes
        zones = {k: round(v / 60, 1) for k, v in zones.items()}
        zones['total_minutes'] = sum(zones.values())
        
        return zones
    
    def extract_power_curve(self, durations: List[int] = None) -> Dict[int, int]:
        """
        Calculate power curve (best average power for various durations)
        
        Args:
            durations: List of durations in seconds to calculate (default: common intervals)
        
        Returns:
            Dictionary mapping duration (seconds) to max avg power (watts)
        """
        if durations is None:
            durations = [5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800, 3600]  # Common power curve intervals
        
        analysis = self.analyze(include_records=True)
        power_data = [r.power for r in analysis.records if r.power]
        
        if not power_data:
            return {}
        
        power_curve = {}
        
        for duration in durations:
            max_avg = 0
            # Sliding window to find best average for this duration
            for i in range(len(power_data) - duration + 1):
                window_avg = sum(power_data[i:i+duration]) / duration
                max_avg = max(max_avg, window_avg)
            
            power_curve[duration] = int(max_avg)
        
        return power_curve


def analyze_fit_file(file_path: str, include_records: bool = True) -> WorkoutAnalysis:
    """
    Convenience function to analyze a FIT file
    
    Args:
        file_path: Path to the .fit file
        include_records: Whether to include time-series data (can be large)
    
    Returns:
        WorkoutAnalysis object with all extracted metrics
    """
    analyzer = FitFileAnalyzer(file_path)
    return analyzer.analyze(include_records=include_records)


def get_fit_summary(file_path: str) -> Dict[str, Any]:
    """
    Convenience function to get quick summary of a FIT file
    
    Args:
        file_path: Path to the .fit file
    
    Returns:
        Dictionary with summary metrics
    """
    analyzer = FitFileAnalyzer(file_path)
    return analyzer.get_quick_summary()
