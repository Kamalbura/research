#!/usr/bin/env python3
"""Battery physics-based predictor with Peukert equation, temperature compensation, and real-time SOC estimation.

This module implements sophisticated battery modeling for UAV mission planning:
- Peukert's equation for non-linear discharge behavior under varying loads
- Temperature compensation for Li-Po performance degradation
- Real-time State of Charge (SOC) estimation using INA219 voltage/current data
- Predictive remaining flight time calculation with power envelope forecasting
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import deque


@dataclass
class BatterySpec:
    """Li-Po battery specification parameters."""
    
    nominal_capacity_ah: float  # Amp-hours at 1C discharge rate
    nominal_voltage_v: float    # Nominal cell voltage (typically 3.7V for Li-Po)
    peukert_exponent: float     # Peukert constant (1.0-1.4 for Li-Po, typically ~1.2)
    series_cells: int           # Number of cells in series (3S = 3, 4S = 4, etc.)
    internal_resistance_mohm: float  # Internal resistance in milliohms
    temp_coeff_percent_per_c: float  # Capacity temperature coefficient (%/°C)
    cutoff_voltage_per_cell_v: float  # Minimum safe voltage per cell
    
    @property
    def total_nominal_voltage_v(self) -> float:
        """Total battery pack nominal voltage."""
        return self.nominal_voltage_v * self.series_cells
    
    @property
    def cutoff_voltage_total_v(self) -> float:
        """Total battery pack cutoff voltage."""
        return self.cutoff_voltage_per_cell_v * self.series_cells


@dataclass
class BatteryState:
    """Current battery state from sensor readings."""
    
    timestamp_ns: int
    voltage_v: float
    current_a: float
    temperature_c: Optional[float] = None
    power_w: Optional[float] = None
    
    def __post_init__(self):
        if self.power_w is None:
            self.power_w = self.voltage_v * self.current_a


@dataclass
class BatteryPrediction:
    """Battery state prediction and health metrics."""
    
    soc_percent: float                    # State of charge (0-100%)
    remaining_capacity_ah: float          # Remaining amp-hours
    remaining_time_s: float               # Time until cutoff at current load
    effective_capacity_ah: float          # Temperature-compensated capacity
    voltage_under_load_v: float           # Predicted voltage accounting for internal resistance
    discharge_rate_c: float               # Current discharge rate (C-rating)
    health_score: float                   # Battery health (0-100%, 100% = new)
    critical_warning: bool                # True if battery critically low
    temperature_derating_factor: float    # Temperature impact on capacity (0-1)


class BatteryPredictor:
    """Real-time battery physics predictor using Peukert equation and temperature compensation."""
    
    def __init__(
        self,
        battery_spec: BatterySpec,
        history_window_s: float = 300.0,  # 5 minutes of history
        critical_soc_threshold: float = 15.0,  # Critical battery warning threshold
        temperature_reference_c: float = 25.0,  # Reference temperature for capacity rating
    ):
        self.spec = battery_spec
        self.history_window_s = history_window_s
        self.critical_threshold = critical_soc_threshold
        self.temp_reference = temperature_reference_c
        
        # Rolling history for trend analysis
        self.state_history: deque[BatteryState] = deque(maxlen=1000)
        
        # Coulomb counting accumulator
        self.cumulative_ah_consumed = 0.0
        self.last_update_ns: Optional[int] = None
        
        # Battery aging model (simplified)
        self.cycle_count = 0
        self.age_factor = 1.0  # 1.0 = new battery, decreases with age
    
    def update(self, state: BatteryState) -> BatteryPrediction:
        """Update battery model with new sensor readings and return prediction."""
        
        # Add to history and prune old entries
        self.state_history.append(state)
        self._prune_history(state.timestamp_ns)
        
        # Update coulomb counter
        self._update_coulomb_counting(state)
        
        # Calculate temperature-compensated capacity
        temp_factor = self._temperature_compensation_factor(state.temperature_c)
        effective_capacity = self.spec.nominal_capacity_ah * temp_factor * self.age_factor
        
        # Calculate discharge rate (C-rating)
        discharge_rate_c = abs(state.current_a) / self.spec.nominal_capacity_ah
        
        # Apply Peukert's equation for non-linear discharge
        peukert_capacity = self._apply_peukert_equation(effective_capacity, discharge_rate_c)
        
        # Calculate State of Charge using voltage and coulomb counting
        voltage_soc = self._voltage_to_soc(state.voltage_v)
        coulomb_soc = max(0.0, 100.0 * (1.0 - self.cumulative_ah_consumed / peukert_capacity))
        
        # Weighted combination (more weight on coulomb counting during discharge)
        if abs(state.current_a) > 0.1:  # Discharging
            soc = 0.3 * voltage_soc + 0.7 * coulomb_soc
        else:  # At rest
            soc = 0.8 * voltage_soc + 0.2 * coulomb_soc
        
        soc = max(0.0, min(100.0, soc))
        
        # Calculate remaining capacity and time
        remaining_ah = (soc / 100.0) * peukert_capacity
        
        if abs(state.current_a) > 0.01:  # Avoid division by zero
            remaining_time_s = (remaining_ah / abs(state.current_a)) * 3600.0
        else:
            remaining_time_s = float('inf')
        
        # Account for voltage drop under load
        voltage_drop = abs(state.current_a) * (self.spec.internal_resistance_mohm / 1000.0)
        voltage_under_load = state.voltage_v - voltage_drop
        
        # Calculate health score based on capacity fade and internal resistance
        health_score = self.age_factor * 100.0
        
        # Critical warning logic
        critical_warning = (
            soc < self.critical_threshold or 
            voltage_under_load < self.spec.cutoff_voltage_total_v or
            state.temperature_c is not None and (state.temperature_c > 60.0 or state.temperature_c < -10.0)
        )
        
        return BatteryPrediction(
            soc_percent=soc,
            remaining_capacity_ah=remaining_ah,
            remaining_time_s=remaining_time_s,
            effective_capacity_ah=effective_capacity,
            voltage_under_load_v=voltage_under_load,
            discharge_rate_c=discharge_rate_c,
            health_score=health_score,
            critical_warning=critical_warning,
            temperature_derating_factor=temp_factor,
        )
    
    def _prune_history(self, current_time_ns: int) -> None:
        """Remove history entries older than the window."""
        cutoff_ns = current_time_ns - int(self.history_window_s * 1e9)
        while self.state_history and self.state_history[0].timestamp_ns < cutoff_ns:
            self.state_history.popleft()
    
    def _update_coulomb_counting(self, state: BatteryState) -> None:
        """Update cumulative amp-hour consumption using trapezoidal integration."""
        if self.last_update_ns is None:
            self.last_update_ns = state.timestamp_ns
            return
        
        dt_s = (state.timestamp_ns - self.last_update_ns) / 1e9
        if dt_s > 0 and dt_s < 3600:  # Sanity check: max 1 hour between updates
            # Only count discharge (positive current)
            if state.current_a > 0:
                ah_delta = state.current_a * dt_s / 3600.0
                self.cumulative_ah_consumed += ah_delta
        
        self.last_update_ns = state.timestamp_ns
    
    def _temperature_compensation_factor(self, temp_c: Optional[float]) -> float:
        """Calculate capacity derating factor due to temperature."""
        if temp_c is None:
            return 1.0
        
        temp_delta = temp_c - self.temp_reference
        # Li-Po batteries lose ~1-2% capacity per degree below 25°C
        # and gain slightly above (but with reduced cycle life)
        if temp_delta < 0:
            # Cold derating: more severe
            factor = 1.0 + (temp_delta * self.spec.temp_coeff_percent_per_c / 100.0)
        else:
            # Warm derating: less impact on capacity but affects longevity
            factor = 1.0 + (temp_delta * self.spec.temp_coeff_percent_per_c * 0.5 / 100.0)
        
        return max(0.3, min(1.2, factor))  # Clamp to reasonable range
    
    def _apply_peukert_equation(self, base_capacity_ah: float, discharge_rate_c: float) -> float:
        """Apply Peukert's equation to account for non-linear discharge behavior."""
        if discharge_rate_c <= 0:
            return base_capacity_ah
        
        # Peukert's equation: Capacity = Rated_Capacity * (Rated_Current/Actual_Current)^(n-1)
        # where n is the Peukert exponent
        peukert_factor = (1.0 / discharge_rate_c) ** (self.spec.peukert_exponent - 1.0)
        return base_capacity_ah * peukert_factor
    
    def _voltage_to_soc(self, voltage_v: float) -> float:
        """Convert battery voltage to approximate State of Charge using discharge curve."""
        # Simplified Li-Po discharge curve (per cell)
        voltage_per_cell = voltage_v / self.spec.series_cells
        
        if voltage_per_cell >= 4.1:
            return 100.0
        elif voltage_per_cell >= 3.9:
            return 90.0 + 10.0 * (voltage_per_cell - 3.9) / 0.2
        elif voltage_per_cell >= 3.8:
            return 70.0 + 20.0 * (voltage_per_cell - 3.8) / 0.1
        elif voltage_per_cell >= 3.7:
            return 40.0 + 30.0 * (voltage_per_cell - 3.7) / 0.1
        elif voltage_per_cell >= 3.6:
            return 20.0 + 20.0 * (voltage_per_cell - 3.6) / 0.1
        elif voltage_per_cell >= 3.4:
            return 5.0 + 15.0 * (voltage_per_cell - 3.4) / 0.2
        else:
            return max(0.0, 5.0 * (voltage_per_cell - 3.0) / 0.4)
    
    def get_power_trend_analysis(self, window_s: float = 60.0) -> Dict[str, float]:
        """Analyze power consumption trends over specified window."""
        if len(self.state_history) < 2:
            return {"trend_w_per_s": 0.0, "avg_power_w": 0.0, "peak_power_w": 0.0}
        
        current_time = self.state_history[-1].timestamp_ns
        cutoff_time = current_time - int(window_s * 1e9)
        
        recent_states = [s for s in self.state_history if s.timestamp_ns >= cutoff_time]
        
        if len(recent_states) < 2:
            return {"trend_w_per_s": 0.0, "avg_power_w": 0.0, "peak_power_w": 0.0}
        
        powers = [s.power_w or 0.0 for s in recent_states]
        avg_power = sum(powers) / len(powers)
        peak_power = max(powers)
        
        # Linear trend calculation
        first_state = recent_states[0]
        last_state = recent_states[-1]
        dt_s = (last_state.timestamp_ns - first_state.timestamp_ns) / 1e9
        
        if dt_s > 0:
            power_trend = ((last_state.power_w or 0.0) - (first_state.power_w or 0.0)) / dt_s
        else:
            power_trend = 0.0
        
        return {
            "trend_w_per_s": power_trend,
            "avg_power_w": avg_power,
            "peak_power_w": peak_power,
        }
    
    def predict_mission_viability(
        self, 
        target_duration_s: float, 
        expected_avg_power_w: float
    ) -> Dict[str, any]:
        """Predict if battery can sustain target mission duration at expected power level."""
        if not self.state_history:
            return {"viable": False, "reason": "no_battery_data"}
        
        latest_state = self.state_history[-1]
        prediction = self.update(latest_state)
        
        # Calculate expected current draw
        expected_current_a = expected_avg_power_w / latest_state.voltage_v
        expected_discharge_rate_c = expected_current_a / self.spec.nominal_capacity_ah
        
        # Apply Peukert and temperature effects
        temp_factor = self._temperature_compensation_factor(latest_state.temperature_c)
        effective_capacity = self.spec.nominal_capacity_ah * temp_factor * self.age_factor
        peukert_capacity = self._apply_peukert_equation(effective_capacity, expected_discharge_rate_c)
        
        # Calculate time to empty at expected power level
        available_ah = prediction.remaining_capacity_ah
        time_to_empty_s = (available_ah / expected_current_a) * 3600.0 if expected_current_a > 0 else float('inf')
        
        viable = time_to_empty_s >= target_duration_s
        margin_s = time_to_empty_s - target_duration_s
        margin_percent = (margin_s / target_duration_s) * 100.0 if target_duration_s > 0 else 0.0
        
        return {
            "viable": viable,
            "time_to_empty_s": time_to_empty_s,
            "target_duration_s": target_duration_s,
            "margin_s": margin_s,
            "margin_percent": margin_percent,
            "expected_power_w": expected_avg_power_w,
            "current_soc_percent": prediction.soc_percent,
            "reason": "insufficient_capacity" if not viable else "viable",
        }


def create_default_lipo_spec(capacity_ah: float, series_cells: int) -> BatterySpec:
    """Create a default Li-Po battery specification."""
    return BatterySpec(
        nominal_capacity_ah=capacity_ah,
        nominal_voltage_v=3.7,
        peukert_exponent=1.15,  # Typical for quality Li-Po
        series_cells=series_cells,
        internal_resistance_mohm=10.0 * series_cells,  # Rough estimate
        temp_coeff_percent_per_c=-1.5,  # 1.5% capacity loss per degree below 25°C
        cutoff_voltage_per_cell_v=3.3,  # Conservative cutoff for longevity
    )


__all__ = [
    "BatterySpec",
    "BatteryState", 
    "BatteryPrediction",
    "BatteryPredictor",
    "create_default_lipo_spec",
]