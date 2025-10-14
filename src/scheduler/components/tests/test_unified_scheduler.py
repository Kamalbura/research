#!/usr/bin/env python3
"""Unit tests for unified scheduler orchestrator."""

import pytest
import time
import threading
from unittest.mock import Mock, patch, MagicMock
from src.scheduler.unified_scheduler import (
    UnifiedUAVScheduler, TelemetrySnapshot, DecisionMetrics, SchedulerConfig
)
from src.scheduler.components.battery_predictor import BatteryPredictor
from src.scheduler.components.thermal_guard import ThermalGuard, ThermalState
from src.scheduler.components.security_advisor import SecurityAdvisor, ThreatLevel 
from src.scheduler.components.ipc_bridge import IPCBridge, AlgorithmType


class TestUnifiedUAVScheduler:
    
    @pytest.fixture
    def mock_components(self):
        """Create mock components for testing."""
        battery_mock = Mock(spec=BatteryPredictor)
        thermal_mock = Mock(spec=ThermalGuard)
        security_mock = Mock(spec=SecurityAdvisor)
        ipc_mock = Mock(spec=IPCBridge)
        
        return {
            'battery': battery_mock,
            'thermal': thermal_mock,
            'security': security_mock,
            'ipc': ipc_mock
        }
    
    def test_scheduler_initialization(self, mock_components):
        """Test scheduler initialization with component injection."""
        config = SchedulerConfig(
            decision_interval_ms=100,
            enable_expert_system=True,
            enable_reinforcement_learning=True,
            enable_hybrid_fusion=True
        )
        
        with patch('src.scheduler.unified_scheduler.UnifiedUAVScheduler._initialize_components'):
            scheduler = UnifiedUAVScheduler(
                config=config,
                battery_predictor=mock_components['battery'],
                thermal_guard=mock_components['thermal'],
                security_advisor=mock_components['security'],
                ipc_bridge=mock_components['ipc']
            )
            
            assert scheduler.config == config
            assert scheduler.battery_predictor == mock_components['battery']
            assert scheduler.thermal_guard == mock_components['thermal']
            assert scheduler.security_advisor == mock_components['security']
            assert scheduler.ipc_bridge == mock_components['ipc']
    
    def test_telemetry_processing(self, mock_components):
        """Test telemetry snapshot processing and validation."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock component responses
        mock_components['battery'].update.return_value = Mock(
            soc_percentage=75.0,
            remaining_time_minutes=45.0,
            power_trend_w_per_s=-0.5
        )
        
        mock_components['thermal'].update.return_value = Mock(
            state=ThermalState.NORMAL,
            current_temp_c=55.0,
            thermal_headroom_c=25.0
        )
        
        mock_components['security'].analyze_network_traffic.return_value = Mock(
            threat_level=ThreatLevel.LOW,
            combined_threat_score=0.15
        )
        
        # Process telemetry
        telemetry = TelemetrySnapshot(
            timestamp_ns=time.time_ns(),
            battery_voltage_v=14.8,
            battery_current_a=-2.5,
            cpu_temp_c=55.0,
            ambient_temp_c=25.0,
            network_packet_rate=150.0,
            network_byte_rate=75000.0
        )
        
        processed = scheduler._process_telemetry(telemetry)
        
        assert processed is not None
        assert hasattr(processed, 'battery_analysis')
        assert hasattr(processed, 'thermal_analysis')
        assert hasattr(processed, 'security_analysis')
    
    def test_expert_system_decision_making(self, mock_components):
        """Test expert system decision logic."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Test normal conditions
        normal_analysis = Mock()
        normal_analysis.battery_analysis.soc_percentage = 80.0
        normal_analysis.thermal_analysis.state = ThermalState.NORMAL
        normal_analysis.security_analysis.threat_level = ThreatLevel.LOW
        
        decision_normal = scheduler._expert_system_decision(normal_analysis)
        
        assert decision_normal.recommended_suite in [
            "cs-mlkem768-aesgcm-mldsa65",  # Balanced choice
            "cs-mlkem1024-aesgcm-mldsa87"  # High security choice
        ]
        assert decision_normal.confidence_score > 0.5
        
        # Test critical battery condition
        critical_analysis = Mock()
        critical_analysis.battery_analysis.soc_percentage = 15.0  # Critical
        critical_analysis.thermal_analysis.state = ThermalState.NORMAL
        critical_analysis.security_analysis.threat_level = ThreatLevel.LOW
        
        decision_critical = scheduler._expert_system_decision(critical_analysis)
        
        # Should prefer low-power suite
        assert decision_critical.recommended_suite == "cs-mlkem512-aesgcm-mldsa44"
        assert "battery_critical" in decision_critical.reasoning
    
    def test_reinforcement_learning_integration(self, mock_components):
        """Test reinforcement learning decision integration."""
        with patch('src.scheduler.unified_scheduler.UnifiedUAVScheduler._load_rl_model') as mock_load:
            mock_rl_model = Mock()
            mock_load.return_value = mock_rl_model
            
            scheduler = UnifiedUAVScheduler(
                battery_predictor=mock_components['battery'],
                thermal_guard=mock_components['thermal'],
                security_advisor=mock_components['security'],
                ipc_bridge=mock_components['ipc']
            )
            
            # Mock RL model prediction
            mock_rl_model.predict.return_value = ([1], [0.85])  # Suite index 1, confidence 0.85
            
            analysis = Mock()
            analysis.battery_analysis.soc_percentage = 60.0
            analysis.thermal_analysis.current_temp_c = 65.0
            analysis.security_analysis.combined_threat_score = 0.3
            
            decision_rl = scheduler._reinforcement_learning_decision(analysis)
            
            assert decision_rl.recommended_suite in scheduler.available_suites
            assert decision_rl.confidence_score == 0.85
            assert decision_rl.algorithm_used == AlgorithmType.REINFORCEMENT_LEARNING
    
    def test_hybrid_fusion_decision_making(self, mock_components):
        """Test hybrid fusion of expert system and RL decisions."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock expert system decision
        expert_decision = Mock()
        expert_decision.recommended_suite = "cs-mlkem768-aesgcm-mldsa65"
        expert_decision.confidence_score = 0.7
        expert_decision.algorithm_used = AlgorithmType.EXPERT_SYSTEM
        
        # Mock RL decision
        rl_decision = Mock()
        rl_decision.recommended_suite = "cs-mlkem1024-aesgcm-mldsa87"
        rl_decision.confidence_score = 0.8
        rl_decision.algorithm_used = AlgorithmType.REINFORCEMENT_LEARNING
        
        with patch.object(scheduler, '_expert_system_decision', return_value=expert_decision):
            with patch.object(scheduler, '_reinforcement_learning_decision', return_value=rl_decision):
                
                analysis = Mock()
                fusion_decision = scheduler._hybrid_fusion_decision(analysis)
                
                # Should choose RL decision due to higher confidence
                assert fusion_decision.recommended_suite == "cs-mlkem1024-aesgcm-mldsa87"
                assert fusion_decision.algorithm_used == AlgorithmType.HYBRID_FUSION
                assert fusion_decision.confidence_score > 0.7
    
    def test_graceful_degradation_logic(self, mock_components):
        """Test graceful degradation under resource constraints."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Test thermal degradation
        thermal_critical_analysis = Mock()
        thermal_critical_analysis.battery_analysis.soc_percentage = 50.0
        thermal_critical_analysis.thermal_analysis.state = ThermalState.CRITICAL
        thermal_critical_analysis.thermal_analysis.current_temp_c = 82.0
        thermal_critical_analysis.security_analysis.threat_level = ThreatLevel.LOW
        
        decision_thermal = scheduler._expert_system_decision(thermal_critical_analysis)
        
        # Should degrade to low-power suite
        assert decision_thermal.recommended_suite == "cs-mlkem512-aesgcm-mldsa44"
        assert "thermal_degradation" in decision_thermal.reasoning
        
        # Test battery + thermal combined stress
        combined_stress_analysis = Mock()
        combined_stress_analysis.battery_analysis.soc_percentage = 20.0  # Low battery
        combined_stress_analysis.thermal_analysis.state = ThermalState.ELEVATED
        combined_stress_analysis.security_analysis.threat_level = ThreatLevel.HIGH  # But high threat
        
        decision_combined = scheduler._expert_system_decision(combined_stress_analysis)
        
        # Should balance security vs resource constraints
        assert decision_combined.recommended_suite in [
            "cs-mlkem512-aesgcm-mldsa44",  # Resource priority
            "cs-mlkem768-aesgcm-mldsa65"   # Compromise choice
        ]
    
    def test_real_time_decision_loop(self, mock_components):
        """Test real-time decision loop with timing constraints."""
        config = SchedulerConfig(decision_interval_ms=50)  # Fast loop
        
        scheduler = UnifiedUAVScheduler(
            config=config,
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock telemetry updates
        mock_telemetry_queue = []
        for i in range(5):
            telemetry = TelemetrySnapshot(
                timestamp_ns=time.time_ns() + i * int(50e6),  # 50ms intervals
                battery_voltage_v=14.8 - i * 0.1,
                battery_current_a=-2.0,
                cpu_temp_c=50.0 + i * 2.0,
                network_packet_rate=100.0
            )
            mock_telemetry_queue.append(telemetry)
        
        decisions_made = []
        
        # Mock decision making to capture results
        original_make_decision = scheduler._make_scheduling_decision
        def mock_make_decision(analysis):
            decision = original_make_decision(analysis)
            decisions_made.append((time.time_ns(), decision))
            return decision
        
        scheduler._make_scheduling_decision = mock_make_decision
        
        # Process telemetry in real-time loop simulation
        for telemetry in mock_telemetry_queue:
            start_time = time.time()
            scheduler._process_telemetry(telemetry)
            processing_time = (time.time() - start_time) * 1000  # ms
            
            # Should meet real-time constraints
            assert processing_time < config.decision_interval_ms
    
    def test_algorithm_switching_coordination(self, mock_components):
        """Test coordination of algorithm switching via IPC."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock IPC bridge responses
        mock_components['ipc'].switch_algorithm.return_value = 2.5  # 2.5ms switch time
        
        # Request algorithm switch
        switch_time = scheduler.switch_algorithm(AlgorithmType.REINFORCEMENT_LEARNING)
        
        # Verify IPC bridge was called
        mock_components['ipc'].switch_algorithm.assert_called_once_with(
            target_algorithm=AlgorithmType.REINFORCEMENT_LEARNING,
            priority_ms=pytest.approx(10, abs=5)  # Default priority
        )
        
        assert switch_time == 2.5
    
    def test_performance_monitoring_and_metrics(self, mock_components):
        """Test performance monitoring and metrics collection."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock component responses for metrics
        mock_components['battery'].update.return_value = Mock(soc_percentage=70.0)
        mock_components['thermal'].update.return_value = Mock(state=ThermalState.NORMAL)
        mock_components['security'].analyze_network_traffic.return_value = Mock(
            threat_level=ThreatLevel.LOW
        )
        
        # Process some telemetry to generate metrics
        for i in range(10):
            telemetry = TelemetrySnapshot(
                timestamp_ns=time.time_ns(),
                battery_voltage_v=14.8,
                battery_current_a=-2.0,
                cpu_temp_c=55.0
            )
            scheduler._process_telemetry(telemetry)
        
        metrics = scheduler.get_performance_metrics()
        
        assert "decisions_made" in metrics
        assert "avg_decision_time_ms" in metrics
        assert "algorithm_switches" in metrics
        assert "uptime_seconds" in metrics
        
        assert metrics["decisions_made"] >= 10
        assert metrics["avg_decision_time_ms"] > 0
    
    def test_configuration_hot_reload(self, mock_components):
        """Test hot reloading of scheduler configuration."""
        initial_config = SchedulerConfig(decision_interval_ms=100)
        
        scheduler = UnifiedUAVScheduler(
            config=initial_config,
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Update configuration
        new_config = SchedulerConfig(
            decision_interval_ms=50,  # Faster decisions
            enable_adaptive_thresholds=True
        )
        
        success = scheduler.update_configuration(new_config)
        
        assert success == True
        assert scheduler.config.decision_interval_ms == 50
        assert scheduler.config.enable_adaptive_thresholds == True
    
    def test_emergency_shutdown_procedure(self, mock_components):
        """Test emergency shutdown procedure under critical conditions."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock emergency conditions
        emergency_analysis = Mock()
        emergency_analysis.battery_analysis.soc_percentage = 5.0  # Critical battery
        emergency_analysis.thermal_analysis.state = ThermalState.EMERGENCY
        emergency_analysis.thermal_analysis.emergency_shutdown = True
        emergency_analysis.security_analysis.emergency_mode = True
        
        # Should trigger emergency procedures
        decision = scheduler._expert_system_decision(emergency_analysis)
        
        assert decision.emergency_actions_required == True
        assert "emergency_shutdown" in decision.reasoning
        assert decision.recommended_suite == "cs-mlkem512-aesgcm-mldsa44"  # Minimal power
    
    def test_multi_threaded_operation(self, mock_components):
        """Test scheduler operation under multi-threaded conditions."""
        scheduler = UnifiedUAVScheduler(
            battery_predictor=mock_components['battery'],
            thermal_guard=mock_components['thermal'],
            security_advisor=mock_components['security'],
            ipc_bridge=mock_components['ipc']
        )
        
        # Mock stable component responses
        mock_components['battery'].update.return_value = Mock(soc_percentage=60.0)
        mock_components['thermal'].update.return_value = Mock(state=ThermalState.NORMAL)
        mock_components['security'].analyze_network_traffic.return_value = Mock(
            threat_level=ThreatLevel.LOW
        )
        
        results = []
        errors = []
        
        def worker_thread(thread_id, iterations):
            try:
                for i in range(iterations):
                    telemetry = TelemetrySnapshot(
                        timestamp_ns=time.time_ns(),
                        battery_voltage_v=14.8,
                        battery_current_a=-2.0,
                        cpu_temp_c=55.0 + thread_id  # Slight variation per thread
                    )
                    
                    processed = scheduler._process_telemetry(telemetry)
                    results.append((thread_id, i, processed is not None))
                    
            except Exception as e:
                errors.append((thread_id, str(e)))
        
        # Launch multiple worker threads
        threads = []
        for i in range(3):
            thread = threading.Thread(target=worker_thread, args=(i, 5))
            threads.append(thread)
            thread.start()
        
        # Wait for completion
        for thread in threads:
            thread.join(timeout=10.0)
        
        # Verify thread safety
        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(results) == 15  # 3 threads × 5 iterations
        successful_operations = [r for r in results if r[2]]
        assert len(successful_operations) == 15


if __name__ == "__main__":
    pytest.main([__file__, "-v"])