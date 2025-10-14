#!/usr/bin/env python3
"""Unit tests for security advisor component."""

import pytest
import time
import numpy as np
from unittest.mock import Mock, patch, MagicMock
from src.scheduler.components.security_advisor import (
    SecurityAdvisor, ThreatLevel, SecurityMetrics, AttackVector
)


class TestSecurityAdvisor:
    
    def test_security_advisor_initialization(self):
        """Test security advisor initialization with default parameters."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            assert advisor.threat_threshold_low == 0.3
            assert advisor.threat_threshold_high == 0.7
            assert advisor.current_threat_level == ThreatLevel.LOW
            assert len(advisor.threat_history) == 0
    
    def test_network_traffic_analysis(self):
        """Test network traffic analysis for DDOS detection."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            # Mock model predictions
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # Normal traffic pattern
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.9, 0.1]])  # Low threat
            advisor.transformer_model.predict.return_value = np.array([[0.95, 0.05]])
            
            traffic_features = {
                'packet_rate': 100.0,
                'byte_rate': 50000.0,
                'unique_src_ips': 10,
                'avg_packet_size': 500.0,
                'tcp_syn_rate': 5.0,
                'udp_rate': 20.0
            }
            
            metrics = advisor.analyze_network_traffic(traffic_features)
            
            assert metrics.threat_level == ThreatLevel.LOW
            assert metrics.xgboost_confidence > 0.8
            assert metrics.transformer_confidence > 0.9
            assert metrics.combined_threat_score < 0.3
    
    def test_high_threat_detection(self):
        """Test detection of high-threat network patterns."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            # Mock high-threat predictions
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.2, 0.8]])  # High threat
            advisor.transformer_model.predict.return_value = np.array([[0.1, 0.9]])
            
            # Suspicious traffic pattern (high packet rate, low diversity)
            traffic_features = {
                'packet_rate': 10000.0,  # Very high
                'byte_rate': 1000000.0,
                'unique_src_ips': 1,     # Single source
                'avg_packet_size': 100.0, # Small packets
                'tcp_syn_rate': 5000.0,  # SYN flood indicators
                'udp_rate': 5000.0
            }
            
            metrics = advisor.analyze_network_traffic(traffic_features)
            
            assert metrics.threat_level == ThreatLevel.HIGH
            assert AttackVector.DDOS_VOLUMETRIC in metrics.detected_vectors
            assert metrics.combined_threat_score > 0.7
    
    def test_threat_level_transitions(self):
        """Test threat level state transitions and hysteresis."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor(
                threat_threshold_low=0.3,
                threat_threshold_high=0.7,
                threat_hysteresis=0.1
            )
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # Start with low threat
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.8, 0.2]])
            advisor.transformer_model.predict.return_value = np.array([[0.9, 0.1]])
            
            traffic_low = {'packet_rate': 100.0, 'byte_rate': 50000.0}
            metrics_low = advisor.analyze_network_traffic(traffic_low)
            assert metrics_low.threat_level == ThreatLevel.LOW
            
            # Escalate to high threat
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.2, 0.8]])
            advisor.transformer_model.predict.return_value = np.array([[0.1, 0.9]])
            
            traffic_high = {'packet_rate': 5000.0, 'byte_rate': 500000.0}
            metrics_high = advisor.analyze_network_traffic(traffic_high)
            assert metrics_high.threat_level == ThreatLevel.HIGH
            
            # Moderate reduction (should stay high due to hysteresis)
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.4, 0.6]])
            advisor.transformer_model.predict.return_value = np.array([[0.3, 0.7]])
            
            traffic_moderate = {'packet_rate': 2000.0, 'byte_rate': 200000.0}
            metrics_moderate = advisor.analyze_network_traffic(traffic_moderate)
            assert metrics_moderate.threat_level == ThreatLevel.HIGH  # Hysteresis keeps it high
    
    def test_attack_vector_classification(self):
        """Test classification of different attack vectors."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.1, 0.9]])
            advisor.transformer_model.predict.return_value = np.array([[0.05, 0.95]])
            
            # Test volumetric attack detection
            volumetric_features = {
                'packet_rate': 20000.0,
                'byte_rate': 2000000.0,
                'unique_src_ips': 100,
                'avg_packet_size': 100.0
            }
            
            metrics_vol = advisor.analyze_network_traffic(volumetric_features)
            assert AttackVector.DDOS_VOLUMETRIC in metrics_vol.detected_vectors
            
            # Test protocol attack detection
            protocol_features = {
                'packet_rate': 1000.0,
                'tcp_syn_rate': 900.0,  # High SYN rate
                'tcp_syn_ack_ratio': 0.1,  # Low ACK response
                'unique_src_ips': 50
            }
            
            metrics_proto = advisor.analyze_network_traffic(protocol_features)
            assert AttackVector.DDOS_PROTOCOL in metrics_proto.detected_vectors
    
    def test_suite_security_recommendation(self):
        """Test PQC suite recommendation based on threat level."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            available_suites = [
                "cs-mlkem512-aesgcm-mldsa44",
                "cs-mlkem768-aesgcm-mldsa65",
                "cs-mlkem1024-aesgcm-mldsa87"
            ]
            
            # Low threat should allow balanced suite
            advisor.current_threat_level = ThreatLevel.LOW
            recommended_low = advisor.recommend_security_suite(available_suites)
            assert recommended_low in available_suites
            
            # High threat should prefer maximum security
            advisor.current_threat_level = ThreatLevel.HIGH
            recommended_high = advisor.recommend_security_suite(available_suites)
            assert recommended_high == "cs-mlkem1024-aesgcm-mldsa87"  # Highest security
    
    def test_threat_confidence_scoring(self):
        """Test confidence scoring for threat assessments."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # High confidence scenario (models agree)
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.9, 0.1]])
            advisor.transformer_model.predict.return_value = np.array([[0.95, 0.05]])
            
            traffic_features = {'packet_rate': 100.0}
            metrics_confident = advisor.analyze_network_traffic(traffic_features)
            
            # Low confidence scenario (models disagree)
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.8, 0.2]])
            advisor.transformer_model.predict.return_value = np.array([[0.3, 0.7]])
            
            metrics_uncertain = advisor.analyze_network_traffic(traffic_features)
            
            assert metrics_confident.combined_confidence > metrics_uncertain.combined_confidence
    
    def test_adaptive_threshold_adjustment(self):
        """Test adaptive adjustment of threat thresholds."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor(adaptive_thresholds=True)
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # Simulate consistent false positives
            for _ in range(10):
                advisor.xgboost_model.predict_proba.return_value = np.array([[0.4, 0.6]])
                advisor.transformer_model.predict.return_value = np.array([[0.5, 0.5]])
                
                traffic = {'packet_rate': 200.0}  # Normal traffic
                advisor.analyze_network_traffic(traffic)
                # Mark as false positive
                advisor.record_false_positive()
            
            # Thresholds should have adjusted upward
            assert advisor.threat_threshold_high > 0.7  # Original threshold
    
    def test_performance_monitoring(self):
        """Test performance monitoring of detection models."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # Mock inference times
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.8, 0.2]])
            advisor.transformer_model.predict.return_value = np.array([[0.9, 0.1]])
            
            start_time = time.time()
            traffic_features = {'packet_rate': 100.0}
            metrics = advisor.analyze_network_traffic(traffic_features)
            
            # Should track inference times
            assert hasattr(metrics, 'xgboost_inference_time_ms')
            assert hasattr(metrics, 'transformer_inference_time_ms')
            assert metrics.xgboost_inference_time_ms >= 0
            assert metrics.transformer_inference_time_ms >= 0
    
    def test_feature_importance_analysis(self):
        """Test feature importance analysis for explainability."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor()
            
            # Mock model with feature importance
            advisor.xgboost_model = Mock()
            advisor.xgboost_model.feature_importances_ = np.array([0.3, 0.2, 0.15, 0.1, 0.25])
            
            importance = advisor.get_feature_importance()
            
            assert len(importance) > 0
            assert all(0 <= score <= 1 for score in importance.values())
            assert abs(sum(importance.values()) - 1.0) < 0.01  # Should sum to ~1
    
    def test_threat_history_analysis(self):
        """Test analysis of threat history patterns."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor(history_window_s=60.0)
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            base_time = time.time()
            
            # Add threat history samples
            threat_levels = [0.1, 0.2, 0.8, 0.9, 0.3, 0.1]  # Attack spike pattern
            for i, threat in enumerate(threat_levels):
                advisor.xgboost_model.predict_proba.return_value = np.array([[1-threat, threat]])
                advisor.transformer_model.predict.return_value = np.array([[1-threat, threat]])
                
                traffic = {'packet_rate': 100.0 * threat}
                metrics = advisor.analyze_network_traffic(traffic)
            
            history_analysis = advisor.get_threat_history_analysis()
            
            assert history_analysis['max_threat_score'] > 0.8
            assert history_analysis['avg_threat_score'] > 0.1
            assert history_analysis['threat_spike_count'] > 0
    
    def test_integration_with_ddos_models(self):
        """Test integration with external DDOS detection models."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models') as mock_load:
            # Mock successful model loading
            mock_load.return_value = None
            
            advisor = SecurityAdvisor(
                xgboost_model_path="tests/fixtures/xgb_model.json",
                transformer_model_path="tests/fixtures/transformer_model.pt"
            )
            
            # Verify models would be loaded from specified paths
            mock_load.assert_called_once()
    
    def test_emergency_response_mode(self):
        """Test emergency response mode during severe attacks."""
        with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
            advisor = SecurityAdvisor(emergency_threshold=0.95)
            
            advisor.xgboost_model = Mock()
            advisor.transformer_model = Mock()
            
            # Simulate severe attack
            advisor.xgboost_model.predict_proba.return_value = np.array([[0.02, 0.98]])
            advisor.transformer_model.predict.return_value = np.array([[0.01, 0.99]])
            
            traffic_severe = {
                'packet_rate': 50000.0,
                'byte_rate': 10000000.0,
                'unique_src_ips': 1,
                'tcp_syn_rate': 25000.0
            }
            
            metrics = advisor.analyze_network_traffic(traffic_severe)
            
            assert metrics.emergency_mode == True
            assert metrics.combined_threat_score > 0.95
            assert AttackVector.DDOS_VOLUMETRIC in metrics.detected_vectors


if __name__ == "__main__":
    pytest.main([__file__, "-v"])