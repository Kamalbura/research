#!/usr/bin/env python3
"""Security advisor that bridges DDOS detection models to PQC suite selection decisions.

This module integrates XGBoost and Transformer-based DDOS detection with cryptographic
suite scheduling, implementing a multi-tier defense strategy:
- Light-weight XGBoost for continuous monitoring (90% F1 score, low CPU)
- Heavy-weight Transformer with attention for confirmation (99.9% accuracy)  
- Dynamic threat level mapping to appropriate PQC security postures
- MQTT-inspired lightweight alert mechanism for GCS notification under congestion
"""

from __future__ import annotations

import time
import json
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
from collections import deque


class ThreatLevel(Enum):
    """DDOS threat classification levels."""
    NONE = "none"               # No threat detected
    SUSPICIOUS = "suspicious"   # Anomalous but not confirmed
    CONFIRMED = "confirmed"     # DDOS confirmed by heavy model
    CRITICAL = "critical"       # Severe ongoing DDOS attack


class DDOSDetectionTier(Enum):
    """Detection model tiers with different performance characteristics."""
    LIGHTWEIGHT = "lightweight"    # XGBoost - Fast, 90% F1, always running
    HEAVYWEIGHT = "heavyweight"    # Transformer - Accurate, 99.9%, on-demand


@dataclass
class NetworkMetrics:
    """Network performance metrics for DDOS detection."""
    timestamp_ns: int
    packet_loss_pct: float
    rtt_avg_ms: float
    rtt_p95_ms: float
    throughput_mbps: float
    goodput_mbps: float
    jitter_ms: Optional[float] = None
    out_of_order_pct: Optional[float] = None
    retransmission_rate: Optional[float] = None
    connection_attempts_per_s: Optional[float] = None


@dataclass 
class DDOSPrediction:
    """DDOS detection prediction with confidence and metadata."""
    threat_level: ThreatLevel
    confidence_score: float         # 0.0-1.0 prediction confidence
    detection_tier: DDOSDetectionTier
    features_used: List[str]       # Feature names used in prediction
    model_latency_ms: float        # Time taken for prediction
    anomaly_scores: Dict[str, float]  # Per-feature anomaly scores
    raw_prediction: Optional[float] = None  # Raw model output if available


@dataclass
class SecurityPosture:
    """Recommended security configuration based on threat assessment."""
    pqc_suite: str                  # Recommended PQC suite
    ddos_detection_tier: DDOSDetectionTier  # Active detection level
    traffic_throttling: bool        # Should throttle traffic?
    alert_frequency_s: float        # How often to send status alerts
    emergency_fallback: bool        # Use emergency low-bandwidth mode?
    confidence_score: float         # Confidence in recommendation
    reasoning: str                  # Human-readable explanation


class SecurityAdvisor:
    """Intelligent security advisor for UAV cryptographic scheduling."""
    
    def __init__(
        self,
        lightweight_threshold: float = 0.7,    # XGBoost anomaly threshold
        heavyweight_threshold: float = 0.85,   # Transformer confirmation threshold  
        escalation_window_s: float = 30.0,     # Time window for threat escalation
        alert_cooldown_s: float = 60.0,        # Min time between GCS alerts
        feature_weights: Optional[Dict[str, float]] = None,
    ):
        self.lightweight_threshold = lightweight_threshold
        self.heavyweight_threshold = heavyweight_threshold
        self.escalation_window_s = escalation_window_s
        self.alert_cooldown_s = alert_cooldown_s
        
        # Feature importance weights for composite scoring
        self.feature_weights = feature_weights or {
            "packet_loss_pct": 0.25,
            "rtt_p95_ms": 0.20,
            "throughput_mbps": 0.15,
            "goodput_mbps": 0.15,
            "jitter_ms": 0.10,
            "out_of_order_pct": 0.10,
            "retransmission_rate": 0.05,
        }
        
        # Detection history for trend analysis
        self.prediction_history: deque[DDOSPrediction] = deque(maxlen=1000)
        self.network_history: deque[NetworkMetrics] = deque(maxlen=1000)
        
        # State tracking
        self.current_threat_level = ThreatLevel.NONE
        self.active_detection_tier = DDOSDetectionTier.LIGHTWEIGHT
        self.last_alert_sent_ns: Optional[int] = None
        self.escalation_start_ns: Optional[int] = None
        
        # Pre-encrypted alert codes for lightweight GCS communication
        self.alert_codes = self._generate_alert_codes()
    
    def analyze_threat(
        self, 
        metrics: NetworkMetrics, 
        lightweight_score: Optional[float] = None,
        heavyweight_score: Optional[float] = None,
    ) -> Tuple[DDOSPrediction, SecurityPosture]:
        """Analyze current threat level and recommend security posture."""
        
        # Store metrics for trend analysis
        self.network_history.append(metrics)
        self._prune_history(metrics.timestamp_ns)
        
        # Generate DDOS prediction
        prediction = self._generate_prediction(
            metrics, lightweight_score, heavyweight_score
        )
        
        # Store prediction 
        self.prediction_history.append(prediction)
        
        # Update threat level with temporal logic
        self._update_threat_level(prediction, metrics.timestamp_ns)
        
        # Generate security posture recommendation
        posture = self._recommend_security_posture(prediction, metrics)
        
        return prediction, posture
    
    def _generate_prediction(
        self,
        metrics: NetworkMetrics,
        lightweight_score: Optional[float],
        heavyweight_score: Optional[float],
    ) -> DDOSPrediction:
        """Generate DDOS prediction from available model scores and metrics."""
        
        start_time = time.time()
        
        # If heavyweight score available, use it with high confidence
        if heavyweight_score is not None:
            threat_level = (
                ThreatLevel.CRITICAL if heavyweight_score > 0.95 else
                ThreatLevel.CONFIRMED if heavyweight_score > self.heavyweight_threshold else
                ThreatLevel.SUSPICIOUS if heavyweight_score > 0.5 else
                ThreatLevel.NONE
            )
            
            prediction = DDOSPrediction(
                threat_level=threat_level,
                confidence_score=heavyweight_score,
                detection_tier=DDOSDetectionTier.HEAVYWEIGHT,
                features_used=["transformer_attention_weights", "sequence_patterns"],
                model_latency_ms=(time.time() - start_time) * 1000,
                anomaly_scores={"heavyweight_score": heavyweight_score},
                raw_prediction=heavyweight_score,
            )
            
        # Otherwise use lightweight score or heuristics
        elif lightweight_score is not None:
            threat_level = (
                ThreatLevel.SUSPICIOUS if lightweight_score > self.lightweight_threshold else
                ThreatLevel.NONE
            )
            
            prediction = DDOSPrediction(
                threat_level=threat_level,
                confidence_score=lightweight_score,
                detection_tier=DDOSDetectionTier.LIGHTWEIGHT,
                features_used=["xgboost_features"],
                model_latency_ms=(time.time() - start_time) * 1000,
                anomaly_scores={"lightweight_score": lightweight_score},
                raw_prediction=lightweight_score,
            )
            
        else:
            # Fallback to heuristic-based detection
            prediction = self._heuristic_prediction(metrics, start_time)
        
        return prediction
    
    def _heuristic_prediction(self, metrics: NetworkMetrics, start_time: float) -> DDOSPrediction:
        """Fallback heuristic DDOS detection when ML models unavailable."""
        
        # Calculate composite anomaly score from network metrics
        anomaly_scores = {}
        composite_score = 0.0
        
        # Packet loss anomaly (threshold: >5%)
        loss_anomaly = min(1.0, metrics.packet_loss_pct / 10.0)
        anomaly_scores["packet_loss"] = loss_anomaly
        composite_score += loss_anomaly * self.feature_weights.get("packet_loss_pct", 0.0)
        
        # RTT anomaly (threshold: >200ms for P95)
        rtt_anomaly = min(1.0, max(0.0, (metrics.rtt_p95_ms - 50.0) / 500.0))
        anomaly_scores["rtt_p95"] = rtt_anomaly
        composite_score += rtt_anomaly * self.feature_weights.get("rtt_p95_ms", 0.0)
        
        # Throughput degradation (expect >5 Mbps normally)
        throughput_anomaly = max(0.0, (5.0 - metrics.throughput_mbps) / 5.0)
        anomaly_scores["throughput"] = throughput_anomaly
        composite_score += throughput_anomaly * self.feature_weights.get("throughput_mbps", 0.0)
        
        # Goodput vs throughput ratio (should be >0.8 normally)
        if metrics.throughput_mbps > 0:
            goodput_ratio = metrics.goodput_mbps / metrics.throughput_mbps
            goodput_anomaly = max(0.0, (0.8 - goodput_ratio) / 0.8)
        else:
            goodput_anomaly = 1.0
        anomaly_scores["goodput_ratio"] = goodput_anomaly
        composite_score += goodput_anomaly * self.feature_weights.get("goodput_mbps", 0.0)
        
        # Determine threat level from composite score
        if composite_score > 0.8:
            threat_level = ThreatLevel.SUSPICIOUS
        elif composite_score > 0.4:
            threat_level = ThreatLevel.SUSPICIOUS
        else:
            threat_level = ThreatLevel.NONE
        
        return DDOSPrediction(
            threat_level=threat_level,
            confidence_score=composite_score,
            detection_tier=DDOSDetectionTier.LIGHTWEIGHT,
            features_used=list(anomaly_scores.keys()),
            model_latency_ms=(time.time() - start_time) * 1000,
            anomaly_scores=anomaly_scores,
            raw_prediction=composite_score,
        )
    
    def _update_threat_level(self, prediction: DDOSPrediction, timestamp_ns: int) -> None:
        """Update current threat level with temporal logic and escalation."""
        
        # Escalation logic: if suspicious detections persist, escalate
        if (prediction.threat_level in {ThreatLevel.SUSPICIOUS, ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}
            and self.escalation_start_ns is None):
            self.escalation_start_ns = timestamp_ns
        
        # Check if we should escalate due to persistent suspicious activity
        if (self.escalation_start_ns is not None and 
            prediction.threat_level == ThreatLevel.SUSPICIOUS):
            
            elapsed_s = (timestamp_ns - self.escalation_start_ns) / 1e9
            if elapsed_s > self.escalation_window_s:
                # Escalate persistent suspicious activity to confirmed
                self.current_threat_level = ThreatLevel.CONFIRMED
                return
        
        # Direct updates for confirmed/critical threats
        if prediction.threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
            self.current_threat_level = prediction.threat_level
            self.escalation_start_ns = None  # Reset escalation timer
        elif prediction.threat_level == ThreatLevel.NONE:
            # Clear threat state
            self.current_threat_level = ThreatLevel.NONE
            self.escalation_start_ns = None
        else:
            # Update to suspicious if not already escalated
            if self.current_threat_level == ThreatLevel.NONE:
                self.current_threat_level = ThreatLevel.SUSPICIOUS
    
    def _recommend_security_posture(
        self, 
        prediction: DDOSPrediction, 
        metrics: NetworkMetrics
    ) -> SecurityPosture:
        """Recommend security configuration based on threat assessment."""
        
        # Map threat level to PQC suite selection
        suite_mapping = {
            ThreatLevel.NONE: "cs-mlkem768-aesgcm-mldsa65",      # Balanced default
            ThreatLevel.SUSPICIOUS: "cs-mlkem768-aesgcm-mldsa65", # Keep balanced for now
            ThreatLevel.CONFIRMED: "cs-mlkem1024-aesgcm-mldsa87", # High security
            ThreatLevel.CRITICAL: "cs-mlkem1024-aesgcm-mldsa87",  # Maximum security
        }
        
        # Detection tier recommendations
        if self.current_threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL}:
            detection_tier = DDOSDetectionTier.HEAVYWEIGHT
        else:
            detection_tier = DDOSDetectionTier.LIGHTWEIGHT
        
        # Traffic throttling logic
        should_throttle = (
            self.current_threat_level in {ThreatLevel.CONFIRMED, ThreatLevel.CRITICAL} or
            metrics.packet_loss_pct > 8.0 or
            metrics.rtt_p95_ms > 300.0
        )
        
        # Alert frequency based on threat level
        alert_frequencies = {
            ThreatLevel.NONE: 300.0,        # 5 minutes when all clear
            ThreatLevel.SUSPICIOUS: 120.0,  # 2 minutes when suspicious
            ThreatLevel.CONFIRMED: 30.0,    # 30 seconds when confirmed
            ThreatLevel.CRITICAL: 10.0,     # 10 seconds when critical
        }
        
        # Emergency fallback for severe conditions
        emergency_fallback = (
            self.current_threat_level == ThreatLevel.CRITICAL or
            metrics.packet_loss_pct > 15.0 or
            metrics.throughput_mbps < 1.0
        )
        
        # Generate reasoning
        reasoning_parts = [
            f"Threat level: {self.current_threat_level.value}",
            f"Detection confidence: {prediction.confidence_score:.2f}",
        ]
        
        if should_throttle:
            reasoning_parts.append("throttling due to high loss/latency")
        if emergency_fallback:
            reasoning_parts.append("emergency fallback due to severe degradation")
        
        reasoning = "; ".join(reasoning_parts)
        
        return SecurityPosture(
            pqc_suite=suite_mapping[self.current_threat_level],
            ddos_detection_tier=detection_tier,
            traffic_throttling=should_throttle,
            alert_frequency_s=alert_frequencies[self.current_threat_level],
            emergency_fallback=emergency_fallback,
            confidence_score=prediction.confidence_score,
            reasoning=reasoning,
        )
    
    def should_send_alert(self, current_time_ns: int) -> bool:
        """Check if it's time to send a status alert to GCS."""
        if self.last_alert_sent_ns is None:
            return True
        
        elapsed_s = (current_time_ns - self.last_alert_sent_ns) / 1e9
        return elapsed_s >= self.alert_cooldown_s
    
    def generate_lightweight_alert(
        self, 
        posture: SecurityPosture, 
        current_time_ns: int
    ) -> Optional[bytes]:
        """Generate lightweight encrypted alert packet for GCS communication."""
        
        if not self.should_send_alert(current_time_ns):
            return None
        
        # Create compact alert payload
        alert_data = {
            "t": int(current_time_ns / 1e6),  # Timestamp in milliseconds
            "tl": self.current_threat_level.value[:1],  # First char of threat level
            "dt": posture.ddos_detection_tier.value[:1],  # First char of detection tier
            "th": 1 if posture.traffic_throttling else 0,
            "ef": 1 if posture.emergency_fallback else 0,
            "c": int(posture.confidence_score * 100),  # Confidence as 0-100
        }
        
        # Use pre-encrypted codes for efficiency
        threat_code = self.alert_codes.get(self.current_threat_level, b"UNKN")
        
        # Combine JSON data with threat code
        json_bytes = json.dumps(alert_data, separators=(',', ':')).encode('utf-8')
        alert_packet = threat_code + b"|" + json_bytes
        
        self.last_alert_sent_ns = current_time_ns
        return alert_packet
    
    def _generate_alert_codes(self) -> Dict[ThreatLevel, bytes]:
        """Generate pre-encrypted alert codes for lightweight communication."""
        # In a real implementation, these would be properly encrypted
        # For now, use simple hash-based codes
        codes = {}
        for threat in ThreatLevel:
            code_str = f"PQC_ALERT_{threat.value.upper()}"
            code_hash = hashlib.md5(code_str.encode()).hexdigest()[:8]
            codes[threat] = code_hash.encode('ascii')
        return codes
    
    def _prune_history(self, current_time_ns: int) -> None:
        """Remove old history entries to manage memory."""
        # Keep last 10 minutes of data
        cutoff_ns = current_time_ns - int(600 * 1e9)
        
        while (self.prediction_history and 
               self.prediction_history[0].timestamp_ns is not None and
               getattr(self.prediction_history[0], 'timestamp_ns', current_time_ns) < cutoff_ns):
            self.prediction_history.popleft()
        
        while (self.network_history and 
               self.network_history[0].timestamp_ns < cutoff_ns):
            self.network_history.popleft()
    
    def get_threat_analysis_summary(self) -> Dict[str, Any]:
        """Get comprehensive threat analysis summary for logging/debugging."""
        recent_predictions = list(self.prediction_history)[-10:]
        
        if not recent_predictions:
            return {"status": "no_data"}
        
        # Calculate recent trends
        threat_levels = [p.threat_level.value for p in recent_predictions]
        confidence_scores = [p.confidence_score for p in recent_predictions]
        
        return {
            "current_threat": self.current_threat_level.value,
            "active_detection_tier": self.active_detection_tier.value,
            "recent_predictions": len(recent_predictions),
            "avg_confidence": sum(confidence_scores) / len(confidence_scores),
            "threat_trend": threat_levels,
            "escalation_active": self.escalation_start_ns is not None,
            "time_since_last_alert_s": (
                (time.time_ns() - self.last_alert_sent_ns) / 1e9 
                if self.last_alert_sent_ns else None
            ),
        }


__all__ = [
    "ThreatLevel",
    "DDOSDetectionTier", 
    "NetworkMetrics",
    "DDOSPrediction",
    "SecurityPosture",
    "SecurityAdvisor",
]