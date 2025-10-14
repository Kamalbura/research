#!/usr/bin/env python3
"""
Unified UAV Scheduler - Version 4.0
====================================

Advanced battery-aware, thermal-conscious, security-adaptive scheduler for 
post-quantum cryptographic UAV command and control systems.

Integrates:
- Physics-based battery modeling with Peukert equation
- Thermal runaway protection with predictive modeling  
- Multi-tier DDOS detection (XGBoost + Transformer)
- POSIX IPC for ultra-low-latency algorithm switching
- Graceful degradation with expert policy fallbacks

Hardware: Raspberry Pi 4 + Pixhawk via MAVLink + INA219 power monitoring
Research: Battery and Temperature-Aware Graceful Degradation Schedulers for PQC UAV C&C
Authors: Kamal et al.
"""

from __future__ import annotations

import time
import json
import threading
import logging
from collections import deque
from dataclasses import dataclass, asdict
from typing import Deque, Dict, List, Optional, Any, Callable
from pathlib import Path
from enum import Enum

# Import our components
from .components.battery_predictor import (
    BatteryPredictor, BatteryState, BatteryPrediction, 
    create_default_lipo_spec
)
from .components.thermal_guard import (
    ThermalGuard, TemperatureSample, ThermalAnalysis, ThermalState
)
from .components.security_advisor import (
    SecurityAdvisor,
    NetworkMetrics,
    SecurityPosture,
    ThreatLevel,
    DDOSPrediction,
)
from .components.ipc_bridge import (
    IPCBridge, create_pqc_suite_bridge, create_ddos_model_bridge
)

# Import existing scheduler strategies
import sys
sys.path.append(str(Path(__file__).parents[3]))
from schedulers.common.state import (
    SchedulerContext,
    SchedulerDecision,
    DdosMode,
    SuiteTelemetry,
    TelemetryWindow,
)
from schedulers.nextgen_expert.strategy import NextGenExpertStrategy
from schedulers.nextgen_rl.strategy import NextGenRlStrategy


class SchedulerMode(Enum):
    """Scheduler operation modes."""
    EXPERT_ONLY = "expert"          # Rule-based expert policies
    RL_ONLY = "rl"                  # Pure reinforcement learning
    HYBRID_ADAPTIVE = "hybrid"      # Adaptive expert+RL fusion
    EMERGENCY_SAFE = "emergency"    # Emergency safe mode


@dataclass
class SystemTelemetry:
    """Consolidated system telemetry from all sensors."""
    timestamp_ns: int
    
    # Battery metrics (from INA219)
    battery_voltage_v: float
    battery_current_a: float
    battery_power_w: float
    
    # Thermal metrics (from system sensors)
    cpu_temp_c: float
    
    battery_temp_c: Optional[float] = None
    gpu_temp_c: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    
    # Network performance metrics
    packet_loss_pct: float = 0.0
    rtt_avg_ms: float = 0.0
    rtt_p95_ms: float = 0.0
    throughput_mbps: float = 0.0
    goodput_mbps: float = 0.0
    
    # System performance metrics
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    cpu_freq_mhz: Optional[float] = None
    
    # Mission context
    altitude_m: Optional[float] = None
    speed_mps: Optional[float] = None
    flight_mode: Optional[str] = None
    # Heartbeat telemetry (from GCS heartbeat summary mapping)
    heartbeat_ok: Optional[bool] = None
    heartbeat_missed_count: Optional[int] = None
    heartbeat_last_ok_step: Optional[int] = None


@dataclass
class SchedulerState:
    """Current scheduler state and decisions."""
    mode: SchedulerMode
    active_suite: str
    active_ddos_tier: str
    battery_soc_percent: float
    thermal_state: ThermalState
    threat_level: ThreatLevel
    last_decision_ns: int
    performance_score: float
    emergency_mode: bool = False


@dataclass 
class SchedulerMetrics:
    """Performance metrics for the scheduler."""
    decisions_per_minute: float
    avg_decision_latency_ms: float
    suite_switches: int
    emergency_activations: int
    battery_warnings: int
    thermal_warnings: int
    ddos_detections: int
    ipc_performance: Dict[str, float]


class UnifiedUAVScheduler:
    """
    Advanced UAV scheduler integrating battery, thermal, and security management.
    
    This is the main orchestrator that coordinates all subsystems to make
    optimal scheduling decisions for post-quantum cryptography in constrained
    UAV environments.
    """
    
    def __init__(
        self,
        battery_capacity_ah: float = 5.0,    # Battery capacity 
        battery_cells: int = 4,              # 4S Li-Po (14.8V nominal)
        log_dir: Path = Path("logging/scheduler"),
        decision_interval_s: float = 2.0,    # Decision cadence
        emergency_battery_pct: float = 15.0, # Emergency battery threshold
        critical_temp_c: float = 80.0,       # Critical temperature
        available_suites: Optional[List[str]] = None,
    ):
        
        # Configuration
        self.decision_interval_s = decision_interval_s
        self.emergency_battery_pct = emergency_battery_pct
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Available PQC suites (ordered by performance/security trade-off)
        self.available_suites = available_suites or [
            "cs-mlkem512-aesgcm-mldsa44",    # Low power, fast
            "cs-mlkem768-aesgcm-mldsa65",    # Balanced
            "cs-mlkem1024-aesgcm-mldsa87",   # High security, power hungry
        ]
        
        # Initialize core components
        battery_spec = create_default_lipo_spec(battery_capacity_ah, battery_cells)
        self.battery_predictor = BatteryPredictor(
            battery_spec=battery_spec,
            critical_soc_threshold=emergency_battery_pct,
        )
        
        self.thermal_guard = ThermalGuard(
            critical_temp_c=critical_temp_c,
            emergency_temp_c=critical_temp_c + 5.0,
        )
        
        self.security_advisor = SecurityAdvisor()
        
        # Initialize IPC bridges for fast algorithm switching
        self.pqc_bridge = create_pqc_suite_bridge(self.available_suites)
        self.ddos_bridge = create_ddos_model_bridge()
        
        # Initialize scheduler strategies
        self.expert_scheduler = NextGenExpertStrategy()
        self.rl_scheduler = NextGenRlStrategy()
        
        # State tracking
        self.current_state = SchedulerState(
            mode=SchedulerMode.HYBRID_ADAPTIVE,
            active_suite=self.available_suites[1],  # Start with balanced suite
            active_ddos_tier="lightweight",
            battery_soc_percent=100.0,
            thermal_state=ThermalState.NORMAL,
            threat_level=ThreatLevel.NONE,
            last_decision_ns=time.time_ns(),
            performance_score=1.0,
        )
        
        self.metrics = SchedulerMetrics(
            decisions_per_minute=0.0,
            avg_decision_latency_ms=0.0,
            suite_switches=0,
            emergency_activations=0,
            battery_warnings=0,
            thermal_warnings=0,
            ddos_detections=0,
            ipc_performance={},
        )
        
        # RL telemetry window
        self._rl_snapshots: Deque[SuiteTelemetry] = deque(maxlen=6)
        
        # Threading and control
        self.running = False
        self.scheduler_thread: Optional[threading.Thread] = None
        self.decision_callbacks: List[Callable[[SchedulerDecision], None]] = []
        
        # Logging
        self.logger = self._setup_logging()
        
        # Expert policy context and dwell timers
        now_ns = time.time_ns()
        self._last_battery_prediction: Optional[BatteryPrediction] = None
        self._last_thermal_analysis: Optional[ThermalAnalysis] = None
        self._last_security_posture: Optional[SecurityPosture] = None
        self._last_network_metrics: Optional[NetworkMetrics] = None
        self._last_telemetry: Optional[SystemTelemetry] = None
        self._last_suite_change_ns: int = now_ns
        self._last_ddos_change_ns: int = now_ns
        self._suite_dwell_ns: int = int(8.0 * 1e9)   # 8s dwell for upgrades
        self._ddos_dwell_ns: int = int(6.0 * 1e9)    # 6s dwell before relaxing tiers

        # Initialize scheduler strategies with context
        self.context = SchedulerContext(
            session_id=f"uav_scheduler_{int(time.time())}",
            role="unified_scheduler",
            initial_suite=self.current_state.active_suite,
        )
        
        self.expert_scheduler.warmup(self.context)
        self.rl_scheduler.warmup(self.context)
        
        self.logger.info("UnifiedUAVScheduler initialized", extra={
            "battery_capacity_ah": battery_capacity_ah,
            "battery_cells": battery_cells,
            "available_suites": len(self.available_suites),
            "decision_interval_s": decision_interval_s,
        })
    
    def start(self) -> None:
        """Start the scheduler main loop."""
        if self.running:
            return
        
        self.running = True
        self.scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="UAVScheduler",
            daemon=False
        )
        self.scheduler_thread.start()
        self.logger.info("Scheduler started")
    
    def stop(self) -> None:
        """Stop the scheduler and clean up resources."""
        if not self.running:
            return
        
        self.running = False
        
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5.0)
        
        # Clean up IPC resources
        self.pqc_bridge.cleanup()
        self.ddos_bridge.cleanup()
        
        self.logger.info("Scheduler stopped")
    
    def update_telemetry(self, telemetry: SystemTelemetry) -> None:
        """Update scheduler with new telemetry data."""
        
        # Update battery model
        battery_state = BatteryState(
            timestamp_ns=telemetry.timestamp_ns,
            voltage_v=telemetry.battery_voltage_v,
            current_a=telemetry.battery_current_a,
            power_w=telemetry.battery_power_w,
            temperature_c=telemetry.battery_temp_c,
        )
        battery_prediction = self.battery_predictor.update(battery_state)
        
        # Update thermal model
        temp_sample = TemperatureSample(
            timestamp_ns=telemetry.timestamp_ns,
            cpu_temp_c=telemetry.cpu_temp_c,
            gpu_temp_c=telemetry.gpu_temp_c,
            ambient_temp_c=telemetry.ambient_temp_c,
        )
        thermal_analysis = self.thermal_guard.update(temp_sample)
        
        # Update security model
        network_metrics = NetworkMetrics(
            timestamp_ns=telemetry.timestamp_ns,
            packet_loss_pct=telemetry.packet_loss_pct,
            rtt_avg_ms=telemetry.rtt_avg_ms,
            rtt_p95_ms=telemetry.rtt_p95_ms,
            throughput_mbps=telemetry.throughput_mbps,
            goodput_mbps=telemetry.goodput_mbps,
        )
        
        # TODO: Integrate actual ML model predictions here
        lightweight_score = self._calculate_lightweight_ddos_score(network_metrics)
        heavyweight_score = None  # Only compute on-demand
        
        ddos_prediction, security_posture = self.security_advisor.analyze_threat(
            network_metrics, lightweight_score, heavyweight_score
        )
        
        # Persist latest telemetry artifacts for decision logic
        self._last_battery_prediction = battery_prediction
        self._last_thermal_analysis = thermal_analysis
        self._last_security_posture = security_posture
        self._last_network_metrics = network_metrics
        self._last_telemetry = telemetry
        self._record_rl_snapshot(telemetry, battery_prediction, thermal_analysis, network_metrics, ddos_prediction)

        # Update internal state
        self.current_state.battery_soc_percent = battery_prediction.soc_percent
        self.current_state.thermal_state = thermal_analysis.state
        self.current_state.threat_level = ddos_prediction.threat_level
        
        if battery_prediction.critical_warning:
            self.metrics.battery_warnings += 1
        if thermal_analysis.state in {ThermalState.CRITICAL, ThermalState.EMERGENCY}:
            self.metrics.thermal_warnings += 1
        if ddos_prediction.threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
            self.metrics.ddos_detections += 1

        # Check for emergency conditions
        emergency_conditions = [
            battery_prediction.critical_warning,
            thermal_analysis.emergency_shutdown,
            ddos_prediction.threat_level == ThreatLevel.CRITICAL,
        ]
        
        if any(emergency_conditions) and not self.current_state.emergency_mode:
            self._activate_emergency_mode()
        elif not any(emergency_conditions) and self.current_state.emergency_mode:
            self._deactivate_emergency_mode()
        
        # Log telemetry update
        self.logger.debug("Telemetry updated", extra={
            "battery_soc": battery_prediction.soc_percent,
            "thermal_state": thermal_analysis.state.value,
            "threat_level": ddos_prediction.threat_level.value,
            "emergency_mode": self.current_state.emergency_mode,
        })
    
    def _record_rl_snapshot(
        self,
        telemetry: SystemTelemetry,
        battery_prediction: BatteryPrediction,
        thermal_analysis: ThermalAnalysis,
        network_metrics: Optional[NetworkMetrics],
        ddos_prediction: DDOSPrediction,
    ) -> None:
        """Record telemetry snapshot for RL / expert strategies."""
        
        ddos_alert = ddos_prediction.threat_level in {
            ThreatLevel.CONFIRMED,
            ThreatLevel.CRITICAL,
        }

        counters: Dict[str, float] = {
            "thermal_trend_c_per_s": float(thermal_analysis.trend_c_per_s),
            "battery_remaining_s": float(battery_prediction.remaining_time_s),
        }

        snapshot = SuiteTelemetry(
            suite_id=self.current_state.active_suite,
            timestamp_ns=telemetry.timestamp_ns,
            battery_pct=battery_prediction.soc_percent,
            battery_voltage_v=telemetry.battery_voltage_v,
            battery_current_a=telemetry.battery_current_a,
            cpu_percent=telemetry.cpu_percent,
            cpu_temp_c=telemetry.cpu_temp_c,
            power_w=telemetry.battery_power_w,
            throughput_mbps=telemetry.throughput_mbps,
            goodput_mbps=telemetry.goodput_mbps,
            packet_loss_pct=network_metrics.packet_loss_pct if network_metrics else None,
            rtt_ms=network_metrics.rtt_p95_ms if network_metrics else None,
            ddos_alert=ddos_alert,
            counters=counters,
        )

        self._rl_snapshots.append(snapshot)
    
    def _scheduler_loop(self) -> None:
        """Main scheduler decision loop."""
        
        last_decision_time = time.time()
        decision_count = 0
        
        while self.running:
            try:
                loop_start = time.time()
                
                # Make scheduling decision
                decision = self._make_scheduling_decision()
                
                if decision:
                    # Apply decision
                    self._apply_decision(decision)
                    
                    # Notify callbacks
                    for callback in self.decision_callbacks:
                        try:
                            callback(decision)
                        except Exception as e:
                            self.logger.error(f"Decision callback failed: {e}")
                    
                    decision_count += 1
                
                # Update performance metrics
                loop_time = time.time() - loop_start
                self._update_performance_metrics(loop_time, decision_count, last_decision_time)
                
                # Sleep until next decision interval
                sleep_time = max(0.0, self.decision_interval_s - loop_time)
                time.sleep(sleep_time)
                
            except Exception as e:
                self.logger.error(f"Scheduler loop error: {e}", exc_info=True)
                time.sleep(1.0)  # Prevent tight error loop
    
    def _make_scheduling_decision(self) -> Optional[SchedulerDecision]:
        """Make intelligent scheduling decision based on current state."""
        
        if self.current_state.emergency_mode:
            return self._make_emergency_decision()
        
        # Use hybrid decision making based on confidence and conditions
        if self.current_state.mode == SchedulerMode.HYBRID_ADAPTIVE:
            return self._make_hybrid_decision()
        elif self.current_state.mode == SchedulerMode.EXPERT_ONLY:
            return self._make_expert_decision()
        elif self.current_state.mode == SchedulerMode.RL_ONLY:
            return self._make_rl_decision()
        else:
            return self._make_emergency_decision()
    
    def _make_hybrid_decision(self) -> Optional[SchedulerDecision]:
        """Make hybrid decision combining expert rules and RL."""
        
        # Get expert recommendation
        expert_decision = self._make_expert_decision()
        
        # Get RL recommendation  
        rl_decision = self._make_rl_decision()
        
        # Decision fusion logic
        if rl_decision and expert_decision:
            # If both agree, use RL decision (likely higher confidence)
            if rl_decision.target_suite == expert_decision.target_suite:
                return rl_decision
            
            # If they disagree, prefer expert in critical situations
            critical_conditions = [
                self.current_state.battery_soc_percent < 25.0,
                self.current_state.thermal_state in {ThermalState.CRITICAL, ThermalState.EMERGENCY},
                self.current_state.threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL},
            ]
            
            if any(critical_conditions):
                self.logger.info("Critical conditions detected, preferring expert decision")
                return expert_decision
            else:
                # Non-critical: use RL if confidence is high enough
                rl_confidence = float(rl_decision.notes.get("confidence", 0.0))
                if rl_confidence > 0.75:
                    return rl_decision
                else:
                    return expert_decision
        
        # Fallback to whichever is available
        return rl_decision or expert_decision
    
    def _make_expert_decision(self) -> Optional[SchedulerDecision]:
        """Make decision using expert rule-based strategy."""
        if not self.available_suites:
            return None

        # Fallback if we do not have fresh telemetry yet
        if not (
            self._last_battery_prediction and
            self._last_thermal_analysis and
            self._last_telemetry
        ):
            fallback_suite = self.available_suites[min(1, len(self.available_suites) - 1)]
            ddos_mode = DdosMode.LIGHTWEIGHT
            if self.current_state.threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
                fallback_suite = self.available_suites[-1]
                ddos_mode = DdosMode.HEAVYWEIGHT
            elif self.current_state.battery_soc_percent < 20.0:
                fallback_suite = self.available_suites[0]
            return SchedulerDecision(
                target_suite=fallback_suite,
                ddos_mode=ddos_mode,
                notes={
                    "strategy": "expert",
                    "reason": "telemetry_fallback",
                }
            )

        battery = self._last_battery_prediction
        thermal = self._last_thermal_analysis
        telemetry = self._last_telemetry
        network = self._last_network_metrics
        posture = self._last_security_posture
        threat = self.current_state.threat_level

        suite_order = self.available_suites
        try:
            current_index = suite_order.index(self.current_state.active_suite)
        except ValueError:
            current_index = max(0, min(len(suite_order) - 1, 1))

        max_index_allowed = len(suite_order) - 1
        constraint_reasons: List[str] = []

        def apply_cap(new_cap: int, reason: str) -> None:
            nonlocal max_index_allowed
            capped_value = max(0, min(len(suite_order) - 1, new_cap))
            if capped_value < max_index_allowed:
                max_index_allowed = capped_value
                constraint_reasons.append(reason)

        # Battery bins with dwell-aware caps
        if battery.soc_percent <= 15.0 or battery.remaining_time_s < 300.0:
            battery_bin = "critical"
            apply_cap(0, "battery_critical")
        elif battery.soc_percent <= 30.0:
            battery_bin = "low"
            apply_cap(1, "battery_low")
        elif battery.soc_percent <= 55.0:
            battery_bin = "moderate"
            apply_cap(len(suite_order) - 1 if len(suite_order) <= 2 else 2, "battery_moderate")
        else:
            battery_bin = "high"

        # Thermal guard constraints
        if thermal.state == ThermalState.EMERGENCY:
            apply_cap(0, "thermal_emergency")
        elif thermal.state == ThermalState.CRITICAL:
            apply_cap(0, "thermal_critical")
        elif thermal.state == ThermalState.ELEVATED:
            apply_cap(1, "thermal_elevated")
            if thermal.trend_c_per_s > 0.5 or (
                thermal.time_to_critical_s is not None and thermal.time_to_critical_s < 180.0
            ):
                apply_cap(0, "thermal_trend")

        # CPU utilization guardrails
        cpu_pct = telemetry.cpu_percent
        if cpu_pct >= 90.0:
            apply_cap(0, "cpu_saturated")
        elif cpu_pct >= 80.0:
            apply_cap(1, "cpu_high")

        # Network congestion guardrails
        if network is not None:
            if (
                network.packet_loss_pct > 12.0 or
                network.rtt_p95_ms > 400.0 or
                network.throughput_mbps < 1.5
            ):
                apply_cap(0, "network_congested")
            elif network.packet_loss_pct > 6.0 or network.rtt_p95_ms > 250.0:
                apply_cap(1, "network_degraded")

        # Target suite preference driven by threat posture
        desired_index = 0
        suite_source = "default"

        # If heartbeat indicates follower is missing heartbeats, play safe by
        # preferring the lowest-power suite unless overridden by high threat.
        hb_missed_threshold = 2
        try:
            hb_ok = telemetry.heartbeat_ok
            hb_missed = telemetry.heartbeat_missed_count or 0
        except Exception:
            hb_ok = None
            hb_missed = 0

        if hb_ok is False or (hb_missed and hb_missed >= hb_missed_threshold):
            # If threat is severe, still prefer security; otherwise pick safe
            if threat in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
                desired_index = len(suite_order) - 1
                suite_source = "threat_high_overrides_heartbeat"
            else:
                desired_index = 0
                suite_source = "heartbeat_missing_safe_mode"

        if posture and posture.pqc_suite in suite_order:
            desired_index = suite_order.index(posture.pqc_suite)
            suite_source = "security_posture"
        else:
            if threat in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
                desired_index = len(suite_order) - 1
                suite_source = "threat_high"
            elif threat == ThreatLevel.SUSPICIOUS:
                desired_index = min(len(suite_order) - 1, 1)
                suite_source = "threat_suspicious"
            else:
                desired_index = min(len(suite_order) - 1, 1)

        target_index = min(desired_index, max_index_allowed)
        now_ns = time.time_ns()
        dwell_blocked = False

        if target_index > current_index:
            if (now_ns - self._last_suite_change_ns) < self._suite_dwell_ns:
                target_index = current_index
                dwell_blocked = True

        target_suite = suite_order[target_index]

        # Map threat level and posture to DDOS mode with hysteresis
        ddos_mode = DdosMode.LIGHTWEIGHT
        ddos_reason = "threat_map"

        if posture:
            if posture.ddos_detection_tier.value == "heavyweight":
                ddos_mode = DdosMode.HEAVYWEIGHT
            elif threat == ThreatLevel.NONE and not posture.traffic_throttling:
                ddos_mode = DdosMode.DISABLED
            else:
                ddos_mode = DdosMode.LIGHTWEIGHT
            ddos_reason = "security_posture"
        else:
            threat_map = {
                ThreatLevel.NONE: DdosMode.LIGHTWEIGHT,
                ThreatLevel.SUSPICIOUS: DdosMode.LIGHTWEIGHT,
                ThreatLevel.CONFIRMED: DdosMode.HEAVYWEIGHT,
                ThreatLevel.CRITICAL: DdosMode.HEAVYWEIGHT,
            }
            ddos_mode = threat_map.get(threat, DdosMode.LIGHTWEIGHT)

        if network and (
            network.packet_loss_pct > 15.0 or
            network.connection_attempts_per_s and network.connection_attempts_per_s > 50
        ):
            if ddos_mode != DdosMode.HEAVYWEIGHT:
                ddos_mode = DdosMode.HEAVYWEIGHT
                ddos_reason = "network_abuse"

        mode_rank = {
            DdosMode.DISABLED: 0,
            DdosMode.LIGHTWEIGHT: 1,
            DdosMode.HEAVYWEIGHT: 2,
        }
        current_ddos_mode = (
            DdosMode.HEAVYWEIGHT if self.current_state.active_ddos_tier == "heavyweight"
            else DdosMode.DISABLED if self.current_state.active_ddos_tier == "disabled"
            else DdosMode.LIGHTWEIGHT
        )
        ddos_dwell_blocked = False

        if mode_rank[ddos_mode] < mode_rank[current_ddos_mode]:
            if (now_ns - self._last_ddos_change_ns) < self._ddos_dwell_ns:
                ddos_mode = current_ddos_mode
                ddos_dwell_blocked = True

        notes = {
            "strategy": "expert",
            "battery_soc": f"{battery.soc_percent:.1f}",
            "battery_bin": battery_bin,
            "thermal_state": thermal.state.value,
            "cpu_pct": f"{cpu_pct:.1f}",
            "threat_level": threat.value,
            "suite_source": suite_source,
            "ddos_reason": ddos_reason,
        }

        if network:
            notes.update({
                "packet_loss_pct": f"{network.packet_loss_pct:.1f}",
                "rtt_p95_ms": f"{network.rtt_p95_ms:.0f}",
            })

        if constraint_reasons:
            notes["constraints"] = ",".join(constraint_reasons)
        else:
            notes["constraints"] = "none"

        if dwell_blocked:
            notes["suite_dwell_blocked"] = "1"
        if ddos_dwell_blocked:
            notes["ddos_dwell_blocked"] = "1"

        return SchedulerDecision(
            target_suite=target_suite,
            ddos_mode=ddos_mode,
            notes=notes,
        )
    
    def _make_rl_decision(self) -> Optional[SchedulerDecision]:
        """Make decision using reinforcement learning strategy."""
        snapshots = list(self._rl_snapshots)
        if len(snapshots) < 2:
            return None

        window = TelemetryWindow(
            snapshots=snapshots,
            window_start_ns=snapshots[0].timestamp_ns,
            window_end_ns=snapshots[-1].timestamp_ns,
        )

        try:
            decision = self.rl_scheduler.decide(
                context=self.context,
                telemetry=window,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.error("RL decision failed: %s", exc, exc_info=True)
            return None

        if decision is None:
            return None

        notes = dict(decision.notes or {})
        notes.setdefault("strategy", "rl")

        return SchedulerDecision(
            target_suite=decision.target_suite,
            ddos_mode=decision.ddos_mode,
            traffic_rate_mbps=decision.traffic_rate_mbps,
            notes=notes,
        )
    
    def _make_emergency_decision(self) -> SchedulerDecision:
        """Make emergency safe decision."""
        return SchedulerDecision(
            target_suite=self.available_suites[0],  # Lowest power suite
            ddos_mode=DdosMode.DISABLED,            # Minimal processing
            notes={
                "strategy": "emergency",
                "reason": "critical_system_state",
            }
        )
    
    def _apply_decision(self, decision: SchedulerDecision) -> None:
        """Apply scheduling decision to the system."""
        
        decision_start = time.time()
        
        # Switch PQC suite if needed
        if decision.target_suite != self.current_state.active_suite:
            success = self.pqc_bridge.switch_algorithm(decision.target_suite)
            if success:
                self.current_state.active_suite = decision.target_suite
                self.context.initial_suite = decision.target_suite
                self.metrics.suite_switches += 1
                self._last_suite_change_ns = time.time_ns()
                self.logger.info(f"Switched to PQC suite: {decision.target_suite}")
            else:
                self.logger.error(f"Failed to switch to suite: {decision.target_suite}")
        
        # Switch DDOS detection tier if needed
        if decision.ddos_mode == DdosMode.HEAVYWEIGHT:
            ddos_tier = "heavyweight"
            ddos_algorithm = "transformer_heavy"
        elif decision.ddos_mode == DdosMode.LIGHTWEIGHT:
            ddos_tier = "lightweight"
            ddos_algorithm = "xgboost_light"
        else:
            ddos_tier = "disabled"
            ddos_algorithm = "heuristic_fallback"

        if ddos_tier != self.current_state.active_ddos_tier:
            success = True
            if ddos_algorithm:
                success = self.ddos_bridge.switch_algorithm(ddos_algorithm)
            if success:
                self.current_state.active_ddos_tier = ddos_tier
                self._last_ddos_change_ns = time.time_ns()
                self.logger.info(f"Switched to DDOS tier: {ddos_tier}")
        
        # Update state
        self.current_state.last_decision_ns = time.time_ns()
        
        # Log decision
        decision_time_ms = (time.time() - decision_start) * 1000
        self.logger.info("Applied scheduling decision", extra={
            "suite": decision.target_suite,
            "ddos_mode": decision.ddos_mode.value,
            "decision_time_ms": decision_time_ms,
            "notes": decision.notes,
        })
    
    def _activate_emergency_mode(self) -> None:
        """Activate emergency safe mode."""
        self.current_state.emergency_mode = True
        self.current_state.mode = SchedulerMode.EMERGENCY_SAFE
        self.metrics.emergency_activations += 1
        
        self.logger.warning("EMERGENCY MODE ACTIVATED", extra={
            "battery_soc": self.current_state.battery_soc_percent,
            "thermal_state": self.current_state.thermal_state.value,
            "threat_level": self.current_state.threat_level.value,
        })
    
    def _deactivate_emergency_mode(self) -> None:
        """Deactivate emergency mode and return to normal operation."""
        self.current_state.emergency_mode = False
        self.current_state.mode = SchedulerMode.HYBRID_ADAPTIVE
        
        self.logger.info("Emergency mode deactivated - returning to normal operation")
    
    def _calculate_lightweight_ddos_score(self, metrics: NetworkMetrics) -> float:
        """Calculate lightweight DDOS anomaly score using heuristics."""
        # Simple heuristic until real XGBoost integration
        score = 0.0
        
        if metrics.packet_loss_pct > 5.0:
            score += 0.3
        if metrics.rtt_p95_ms > 200.0:
            score += 0.2
        if metrics.throughput_mbps < 2.0:
            score += 0.3
        if metrics.goodput_mbps < metrics.throughput_mbps * 0.8:
            score += 0.2
        
        return min(1.0, score)
    
    def _update_performance_metrics(
        self, 
        loop_time_s: float, 
        decision_count: int, 
        last_decision_time: float
    ) -> None:
        """Update scheduler performance metrics."""
        
        # Decision rate
        time_elapsed = time.time() - last_decision_time
        if time_elapsed > 0:
            self.metrics.decisions_per_minute = (decision_count / time_elapsed) * 60.0
        
        # Average decision latency
        self.metrics.avg_decision_latency_ms = loop_time_s * 1000.0
        
        # IPC performance
        pqc_stats = self.pqc_bridge.get_performance_stats()
        ddos_stats = self.ddos_bridge.get_performance_stats()
        
        self.metrics.ipc_performance = {
            "pqc_avg_switch_ms": pqc_stats.avg_switch_time_ms,
            "pqc_cache_hit_rate": (
                pqc_stats.cache_hits / max(1, pqc_stats.cache_hits + pqc_stats.cache_misses)
            ),
            "ddos_avg_switch_ms": ddos_stats.avg_switch_time_ms,
            "ddos_cache_hit_rate": (
                ddos_stats.cache_hits / max(1, ddos_stats.cache_hits + ddos_stats.cache_misses)
            ),
        }
    
    def _setup_logging(self) -> logging.Logger:
        """Setup structured logging for the scheduler."""
        
        logger = logging.getLogger("UnifiedUAVScheduler")
        logger.setLevel(logging.INFO)
        
        # Create log file with timestamp
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_file = self.log_dir / f"scheduler_{timestamp}.log"
        
        # File handler with JSON formatting
        file_handler = logging.FileHandler(log_file)
        file_formatter = logging.Formatter(
            '{"timestamp":"%(asctime)s","level":"%(levelname)s","message":"%(message)s","extra":%(extra)s}'
        )
        
        # Add custom filter to ensure 'extra' field exists
        class ExtraFilter(logging.Filter):
            def filter(self, record):
                if not hasattr(record, 'extra'):
                    record.extra = '{}'
                else:
                    record.extra = json.dumps(getattr(record, 'extra', {}))
                return True
        
        file_handler.addFilter(ExtraFilter())
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
        
        # Console handler for immediate feedback
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s'
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        
        return logger
    
    def register_decision_callback(self, callback: Callable[[SchedulerDecision], None]) -> None:
        """Register callback to be notified of scheduling decisions."""
        self.decision_callbacks.append(callback)
    
    def get_current_state(self) -> SchedulerState:
        """Get current scheduler state."""
        return self.current_state
    
    def get_performance_metrics(self) -> SchedulerMetrics:
        """Get scheduler performance metrics."""
        return self.metrics
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


__all__ = [
    "UnifiedUAVScheduler",
    "SystemTelemetry", 
    "SchedulerState",
    "SchedulerMetrics",
    "SchedulerMode",
]
