#!/usr/bin/env python3
"""Thermal guard and temperature-aware scheduling for UAV companion computers.

This module implements sophisticated thermal management for Raspberry Pi systems:
- Real-time temperature trend analysis with gradient computation
- Critical threshold detection with hysteresis to prevent oscillation  
- Emergency thermal throttling with graceful degradation to lower-power PQC suites
- Predictive thermal modeling to prevent runaway conditions before they occur
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from collections import deque
from enum import Enum


class ThermalState(Enum):
    """Thermal protection states."""
    NORMAL = "normal"          # < warning threshold
    ELEVATED = "elevated"      # > warning, < critical  
    CRITICAL = "critical"      # > critical threshold
    EMERGENCY = "emergency"    # Rapid temperature rise or > emergency threshold


@dataclass
class TemperatureSample:
    """Single temperature measurement with metadata."""
    timestamp_ns: int
    cpu_temp_c: float
    ambient_temp_c: Optional[float] = None
    gpu_temp_c: Optional[float] = None
    pmic_temp_c: Optional[float] = None  # Power management IC temperature
    
    @property
    def max_temp_c(self) -> float:
        """Maximum temperature across all sensors."""
        temps = [self.cpu_temp_c]
        if self.ambient_temp_c is not None:
            temps.append(self.ambient_temp_c)
        if self.gpu_temp_c is not None:
            temps.append(self.gpu_temp_c)
        if self.pmic_temp_c is not None:
            temps.append(self.pmic_temp_c)
        return max(temps)


@dataclass 
class ThermalAnalysis:
    """Thermal analysis results and recommendations."""
    state: ThermalState
    current_temp_c: float
    trend_c_per_s: float               # Temperature rise rate
    time_to_critical_s: Optional[float] # Predicted time until critical temp
    recommended_action: str             # Action recommendation
    thermal_headroom_c: float          # Distance to critical threshold
    throttling_recommended: bool       # Should system throttle?
    emergency_shutdown: bool           # Should system emergency stop?
    confidence_score: float            # Prediction confidence (0-1)


class ThermalGuard:
    """Temperature-aware thermal protection and scheduling advisor."""
    
    def __init__(
        self,
        warning_temp_c: float = 70.0,      # Start monitoring closely
        critical_temp_c: float = 80.0,     # Begin throttling actions  
        emergency_temp_c: float = 85.0,    # Emergency shutdown threshold
        hysteresis_c: float = 5.0,         # Hysteresis band to prevent oscillation
        trend_window_s: float = 30.0,      # Window for trend analysis
        rapid_rise_threshold_c_per_s: float = 2.0,  # Emergency rise rate
        history_retention_s: float = 600.0, # Keep 10 minutes of history
    ):
        self.warning_temp = warning_temp_c
        self.critical_temp = critical_temp_c  
        self.emergency_temp = emergency_temp_c
        self.hysteresis = hysteresis_c
        self.trend_window_s = trend_window_s
        self.rapid_rise_threshold = rapid_rise_threshold_c_per_s
        self.history_retention_s = history_retention_s
        
        # Temperature history for trend analysis
        self.temp_history: deque[TemperatureSample] = deque(maxlen=2000)
        
        # State tracking
        self.current_state = ThermalState.NORMAL
        self.last_state_change_ns: Optional[int] = None
        self.throttle_start_ns: Optional[int] = None
        
        # Thermal model parameters (simple linear model)
        self.thermal_mass_j_per_c = 10.0    # Thermal mass of system
        self.cooling_rate_w_per_c = 0.5     # Passive cooling effectiveness
    
    def update(self, sample: TemperatureSample) -> ThermalAnalysis:
        """Update thermal model and return analysis with recommendations."""
        
        # Add sample to history and prune old data
        self.temp_history.append(sample)
        self._prune_history(sample.timestamp_ns)
        
        # Calculate temperature trend
        trend_c_per_s = self._calculate_temperature_trend()
        
        # Determine thermal state with hysteresis
        new_state = self._determine_thermal_state(sample.cpu_temp_c, trend_c_per_s)
        
        # Update state tracking
        if new_state != self.current_state:
            self.last_state_change_ns = sample.timestamp_ns
            self.current_state = new_state
        
        # Calculate time to critical temperature
        time_to_critical = self._predict_time_to_critical(sample.cpu_temp_c, trend_c_per_s)
        
        # Generate recommendations
        action, throttling, emergency = self._generate_recommendations(new_state, trend_c_per_s)
        
        # Calculate thermal headroom
        headroom = self.critical_temp - sample.cpu_temp_c
        
        # Confidence score based on trend stability and data quality
        confidence = self._calculate_confidence()
        
        return ThermalAnalysis(
            state=new_state,
            current_temp_c=sample.cpu_temp_c,
            trend_c_per_s=trend_c_per_s,
            time_to_critical_s=time_to_critical,
            recommended_action=action,
            thermal_headroom_c=headroom,
            throttling_recommended=throttling,
            emergency_shutdown=emergency,
            confidence_score=confidence,
        )
    
    def _prune_history(self, current_time_ns: int) -> None:
        """Remove temperature samples older than retention period."""
        cutoff_ns = current_time_ns - int(self.history_retention_s * 1e9)
        while self.temp_history and self.temp_history[0].timestamp_ns < cutoff_ns:
            self.temp_history.popleft()
    
    def _calculate_temperature_trend(self) -> float:
        """Calculate temperature rise rate using linear regression over trend window."""
        if len(self.temp_history) < 3:
            return 0.0
        
        # Get samples within trend window
        latest_time = self.temp_history[-1].timestamp_ns
        cutoff_time = latest_time - int(self.trend_window_s * 1e9)
        
        trend_samples = [s for s in self.temp_history if s.timestamp_ns >= cutoff_time]
        
        if len(trend_samples) < 3:
            return 0.0
        
        # Simple linear regression: y = mx + b, solve for slope m
        n = len(trend_samples)
        sum_t = sum((s.timestamp_ns - trend_samples[0].timestamp_ns) / 1e9 for s in trend_samples)
        sum_temp = sum(s.cpu_temp_c for s in trend_samples)
        sum_t_temp = sum(
            ((s.timestamp_ns - trend_samples[0].timestamp_ns) / 1e9) * s.cpu_temp_c 
            for s in trend_samples
        )
        sum_t_sq = sum(
            ((s.timestamp_ns - trend_samples[0].timestamp_ns) / 1e9) ** 2 
            for s in trend_samples
        )
        
        # Calculate slope (°C/s)
        denominator = n * sum_t_sq - sum_t * sum_t
        if abs(denominator) < 1e-9:
            return 0.0
        
        slope = (n * sum_t_temp - sum_t * sum_temp) / denominator
        return slope
    
    def _determine_thermal_state(self, temp_c: float, trend_c_per_s: float) -> ThermalState:
        """Determine thermal state with hysteresis and trend consideration."""
        
        # Check for emergency rapid temperature rise
        if trend_c_per_s > self.rapid_rise_threshold:
            return ThermalState.EMERGENCY
        
        # Emergency temperature threshold
        if temp_c >= self.emergency_temp:
            return ThermalState.EMERGENCY
        
        # Apply hysteresis based on current state
        if self.current_state == ThermalState.CRITICAL:
            # Need to drop below critical - hysteresis to transition down
            if temp_c < (self.critical_temp - self.hysteresis):
                return ThermalState.ELEVATED if temp_c >= self.warning_temp else ThermalState.NORMAL
            else:
                return ThermalState.CRITICAL
        
        elif self.current_state == ThermalState.ELEVATED:
            # Hysteresis for both up and down transitions
            if temp_c >= self.critical_temp:
                return ThermalState.CRITICAL
            elif temp_c < (self.warning_temp - self.hysteresis):
                return ThermalState.NORMAL
            else:
                return ThermalState.ELEVATED
        
        else:  # NORMAL or EMERGENCY
            # Standard thresholds for upward transitions
            if temp_c >= self.critical_temp:
                return ThermalState.CRITICAL
            elif temp_c >= self.warning_temp:
                return ThermalState.ELEVATED
            else:
                return ThermalState.NORMAL
    
    def _predict_time_to_critical(self, current_temp_c: float, trend_c_per_s: float) -> Optional[float]:
        """Predict time until critical temperature is reached."""
        if trend_c_per_s <= 0:
            return None  # Temperature stable or decreasing
        
        temp_delta = self.critical_temp - current_temp_c
        if temp_delta <= 0:
            return 0.0  # Already at/above critical
        
        # Simple linear extrapolation (conservative)
        time_to_critical = temp_delta / trend_c_per_s
        
        # Cap at reasonable maximum (30 minutes)
        return min(time_to_critical, 1800.0)
    
    def _generate_recommendations(
        self, 
        state: ThermalState, 
        trend_c_per_s: float
    ) -> Tuple[str, bool, bool]:
        """Generate action recommendations based on thermal state."""
        
        if state == ThermalState.EMERGENCY:
            return (
                "EMERGENCY: Immediate shutdown or switch to minimal power suite",
                True,   # throttling_recommended
                True,   # emergency_shutdown  
            )
        
        elif state == ThermalState.CRITICAL:
            if trend_c_per_s > 0.5:  # Still rising
                return (
                    "CRITICAL: Switch to low-power PQC suite immediately", 
                    True, 
                    False
                )
            else:
                return (
                    "CRITICAL: Maintain current low-power configuration",
                    True,
                    False
                )
        
        elif state == ThermalState.ELEVATED:
            if trend_c_per_s > 1.0:  # Rapid rise
                return (
                    "ELEVATED: Preemptively reduce to medium-power suite",
                    True,
                    False
                )
            else:
                return (
                    "ELEVATED: Monitor closely, consider power reduction",
                    False,
                    False
                )
        
        else:  # NORMAL
            return (
                "NORMAL: Full performance available",
                False,
                False
            )
    
    def _calculate_confidence(self) -> float:
        """Calculate confidence in thermal predictions based on data quality."""
        if len(self.temp_history) < 5:
            return 0.3  # Low confidence with insufficient data
        
        # Check temperature measurement stability
        recent_temps = [s.cpu_temp_c for s in list(self.temp_history)[-10:]]
        temp_variance = sum((t - sum(recent_temps)/len(recent_temps))**2 for t in recent_temps) / len(recent_temps)
        temp_stability = max(0.0, 1.0 - temp_variance / 25.0)  # Normalized by 5°C std dev
        
        # Check sampling regularity
        if len(self.temp_history) >= 2:
            intervals = [
                (self.temp_history[i].timestamp_ns - self.temp_history[i-1].timestamp_ns) / 1e9
                for i in range(1, min(len(self.temp_history), 11))
            ]
            avg_interval = sum(intervals) / len(intervals)
            interval_variance = sum((t - avg_interval)**2 for t in intervals) / len(intervals)
            sampling_regularity = max(0.0, 1.0 - interval_variance / (avg_interval**2))
        else:
            sampling_regularity = 0.5
        
        # Combined confidence score
        confidence = (temp_stability * 0.6 + sampling_regularity * 0.4)
        return max(0.1, min(1.0, confidence))
    
    def get_thermal_budget_analysis(self, target_power_increase_w: float) -> Dict[str, any]:
        """Analyze if system can handle additional power load thermally."""
        if not self.temp_history:
            return {"feasible": False, "reason": "no_thermal_data"}
        
        latest_sample = self.temp_history[-1]
        current_analysis = self.update(latest_sample)
        
        # Estimate temperature rise from additional power
        # Rough estimate: 1W additional power = ~2-3°C temperature rise for Pi 4
        estimated_temp_rise_c = target_power_increase_w * 2.5
        projected_temp_c = current_analysis.current_temp_c + estimated_temp_rise_c
        
        # Check thermal headroom
        headroom_after_increase = self.critical_temp - projected_temp_c
        
        feasible = (
            projected_temp_c < self.warning_temp and 
            headroom_after_increase > 5.0 and
            current_analysis.state in {ThermalState.NORMAL, ThermalState.ELEVATED}
        )
        
        return {
            "feasible": feasible,
            "current_temp_c": current_analysis.current_temp_c,
            "projected_temp_c": projected_temp_c,
            "estimated_rise_c": estimated_temp_rise_c,
            "thermal_headroom_c": headroom_after_increase,
            "current_state": current_analysis.state.value,
            "reason": "insufficient_headroom" if not feasible else "feasible",
        }
    
    def get_suite_thermal_mapping(self) -> Dict[str, Dict[str, float]]:
        """Return thermal characteristics for different PQC suites based on observations."""
        # These would ideally be learned from historical data
        # For now, provide reasonable estimates based on computational complexity
        return {
            "cs-mlkem512-aesgcm-mldsa44": {
                "typical_power_w": 2.8,
                "peak_power_w": 4.2,
                "temp_rise_steady_c": 5.0,
                "temp_rise_peak_c": 8.0,
            },
            "cs-mlkem768-aesgcm-mldsa65": {
                "typical_power_w": 3.5,
                "peak_power_w": 5.1,
                "temp_rise_steady_c": 7.0,
                "temp_rise_peak_c": 11.0,
            },
            "cs-mlkem1024-aesgcm-mldsa87": {
                "typical_power_w": 4.8,
                "peak_power_w": 7.2,
                "temp_rise_steady_c": 12.0,
                "temp_rise_peak_c": 18.0,
            },
        }
    
    def recommend_optimal_suite(
        self, 
        available_suites: List[str], 
        current_temp_c: float,
        target_margin_c: float = 10.0
    ) -> Optional[str]:
        """Recommend optimal PQC suite based on current thermal state."""
        thermal_mapping = self.get_suite_thermal_mapping()
        
        # Filter suites that won't cause thermal issues
        viable_suites = []
        for suite in available_suites:
            if suite not in thermal_mapping:
                continue
            
            suite_info = thermal_mapping[suite]
            projected_temp = current_temp_c + suite_info["temp_rise_steady_c"]
            
            if projected_temp < (self.critical_temp - target_margin_c):
                viable_suites.append((suite, projected_temp, suite_info["typical_power_w"]))
        
        if not viable_suites:
            return None
        
        # Sort by projected temperature (ascending) then by power (descending for better security)
        viable_suites.sort(key=lambda x: (x[1], -x[2]))
        
        return viable_suites[0][0]  # Return best option


__all__ = [
    "ThermalState",
    "TemperatureSample", 
    "ThermalAnalysis",
    "ThermalGuard",
]