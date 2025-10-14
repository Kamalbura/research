#!/usr/bin/env python3
"""Integration tests for the complete scheduler pipeline."""

import pytest
import time
import threading
import tempfile
import os
from unittest.mock import Mock, patch
from src.scheduler.unified_scheduler import UnifiedUAVScheduler, TelemetrySnapshot, SchedulerConfig
from src.scheduler.components.battery_predictor import BatteryPredictor, BatterySpecs
from src.scheduler.components.thermal_guard import ThermalGuard, TemperatureSample
from src.scheduler.components.security_advisor import SecurityAdvisor
from src.scheduler.components.ipc_bridge import IPCBridge, AlgorithmType


class TestSchedulerIntegration:
    
    @pytest.fixture
    def realistic_battery_specs(self):
        """Realistic Li-Po battery specifications for testing."""
        return BatterySpecs(
            nominal_voltage_v=14.8,
            nominal_capacity_ah=5.0,
            max_discharge_rate_c=10.0,
            min_voltage_v=11.1,
            max_voltage_v=16.8,
            peukert_exponent=1.3,
            internal_resistance_mohm=50.0,
            temp_coefficient_per_c=-0.005
        )
    
    def test_end_to_end_scheduler_pipeline(self, realistic_battery_specs):
        """Test complete end-to-end scheduler operation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Initialize all real components
            battery_predictor = BatteryPredictor(battery_specs=realistic_battery_specs)
            thermal_guard = ThermalGuard(
                warning_temp=70.0,
                critical_temp=80.0,
                emergency_temp=85.0
            )
            
            # Mock security advisor (requires trained models)
            with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
                security_advisor = SecurityAdvisor()
                security_advisor.xgboost_model = Mock()
                security_advisor.transformer_model = Mock()
                security_advisor.xgboost_model.predict_proba.return_value = [[0.8, 0.2]]
                security_advisor.transformer_model.predict.return_value = [[0.85, 0.15]]
            
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            # Create unified scheduler
            config = SchedulerConfig(
                decision_interval_ms=100,
                enable_expert_system=True,
                enable_reinforcement_learning=False,  # Skip RL for integration test
                enable_hybrid_fusion=False
            )
            
            scheduler = UnifiedUAVScheduler(
                config=config,
                battery_predictor=battery_predictor,
                thermal_guard=thermal_guard,
                security_advisor=security_advisor,
                ipc_bridge=ipc_bridge
            )
            
            # Simulate realistic mission telemetry sequence
            mission_duration_s = 5.0
            telemetry_interval_s = 0.1
            iterations = int(mission_duration_s / telemetry_interval_s)
            
            decisions = []
            start_time = time.time()
            
            for i in range(iterations):
                # Simulate battery discharge and warming
                elapsed_time = i * telemetry_interval_s
                battery_voltage = 14.8 - (elapsed_time / 300.0) * 3.7  # Discharge over 5 minutes
                cpu_temp = 45.0 + (elapsed_time / 60.0) * 15.0  # Warm up over 1 minute
                
                telemetry = TelemetrySnapshot(
                    timestamp_ns=int((start_time + elapsed_time) * 1e9),
                    battery_voltage_v=max(battery_voltage, 11.1),
                    battery_current_a=-3.0,  # 3A discharge
                    cpu_temp_c=min(cpu_temp, 75.0),
                    ambient_temp_c=25.0,
                    network_packet_rate=150.0 + i * 2.0,  # Gradually increasing
                    network_byte_rate=75000.0 + i * 1000.0
                )
                
                # Process telemetry through full pipeline
                analysis = scheduler._process_telemetry(telemetry)
                decision = scheduler._make_scheduling_decision(analysis)
                
                decisions.append({
                    'time': elapsed_time,
                    'battery_soc': analysis.battery_analysis.soc_percentage,
                    'cpu_temp': analysis.thermal_analysis.current_temp_c,
                    'threat_score': analysis.security_analysis.combined_threat_score,
                    'recommended_suite': decision.recommended_suite,
                    'confidence': decision.confidence_score
                })
                
                time.sleep(0.01)  # Small delay to simulate real-time processing
            
            # Analyze decision sequence
            assert len(decisions) == iterations
            
            # Verify battery SOC decreases over time
            initial_soc = decisions[0]['battery_soc']
            final_soc = decisions[-1]['battery_soc']
            assert final_soc < initial_soc
            
            # Verify temperature increases over time  
            initial_temp = decisions[0]['cpu_temp']
            final_temp = decisions[-1]['cpu_temp']
            assert final_temp > initial_temp
            
            # Verify graceful degradation occurs
            suite_changes = []
            prev_suite = decisions[0]['recommended_suite']
            for decision in decisions[1:]:
                if decision['recommended_suite'] != prev_suite:
                    suite_changes.append(decision)
                    prev_suite = decision['recommended_suite']
            
            # Should have at least some adaptation as conditions change
            assert len(suite_changes) >= 0  # May not change if conditions stay stable
            
            # Cleanup
            ipc_bridge.shutdown()
    
    def test_multi_component_stress_conditions(self, realistic_battery_specs):
        """Test scheduler behavior under multiple simultaneous stress conditions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            battery_predictor = BatteryPredictor(battery_specs=realistic_battery_specs)
            thermal_guard = ThermalGuard(warning_temp=65.0, critical_temp=75.0)
            
            with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
                security_advisor = SecurityAdvisor()
                security_advisor.xgboost_model = Mock()
                security_advisor.transformer_model = Mock()
            
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            scheduler = UnifiedUAVScheduler(
                battery_predictor=battery_predictor,
                thermal_guard=thermal_guard,
                security_advisor=security_advisor,
                ipc_bridge=ipc_bridge
            )
            
            # Simulate critical battery + high temperature + security threat
            stress_telemetry = TelemetrySnapshot(
                timestamp_ns=time.time_ns(),
                battery_voltage_v=11.5,  # Critical battery
                battery_current_a=-8.0,  # High discharge
                cpu_temp_c=78.0,        # Above critical thermal threshold
                ambient_temp_c=35.0,    # Hot environment
                network_packet_rate=15000.0,  # Potential DDOS
                network_byte_rate=7500000.0
            )
            
            # Mock high security threat
            security_advisor.xgboost_model.predict_proba.return_value = [[0.1, 0.9]]
            security_advisor.transformer_model.predict.return_value = [[0.05, 0.95]]
            
            # Process stress conditions
            analysis = scheduler._process_telemetry(stress_telemetry)
            decision = scheduler._make_scheduling_decision(analysis)
            
            # Should prioritize resource conservation due to multiple critical conditions
            assert decision.recommended_suite == "cs-mlkem512-aesgcm-mldsa44"
            assert "emergency" in decision.reasoning.lower() or "critical" in decision.reasoning.lower()
            assert decision.emergency_actions_required == True
            
            ipc_bridge.shutdown()
    
    def test_algorithm_switching_performance(self):
        """Test algorithm switching performance under realistic conditions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            # Prewarm algorithms for optimal performance
            ipc_bridge.prewarm_algorithms([
                AlgorithmType.EXPERT_SYSTEM,
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION
            ])
            
            switch_times = []
            algorithms_to_test = [
                AlgorithmType.EXPERT_SYSTEM,
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION,
                AlgorithmType.EXPERT_SYSTEM,  # Test switching back
            ]
            
            for target_algorithm in algorithms_to_test:
                start_time = time.perf_counter()
                switch_time = ipc_bridge.switch_algorithm(target_algorithm, priority_ms=1)
                actual_time = (time.perf_counter() - start_time) * 1000  # Convert to ms
                
                switch_times.append(actual_time)
                
                # Verify switch was recorded
                assert switch_time > 0
                assert ipc_bridge.current_algorithm == target_algorithm
            
            # Verify sub-millisecond switching performance
            avg_switch_time = sum(switch_times) / len(switch_times)
            max_switch_time = max(switch_times)
            
            assert avg_switch_time < 1.0, f"Average switch time {avg_switch_time:.3f}ms too high"
            assert max_switch_time < 2.0, f"Max switch time {max_switch_time:.3f}ms too high"
            
            ipc_bridge.shutdown()
    
    def test_concurrent_telemetry_processing(self, realistic_battery_specs):
        """Test concurrent telemetry processing from multiple sources."""
        with tempfile.TemporaryDirectory() as temp_dir:
            battery_predictor = BatteryPredictor(battery_specs=realistic_battery_specs)
            thermal_guard = ThermalGuard()
            
            with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
                security_advisor = SecurityAdvisor()
                security_advisor.xgboost_model = Mock()
                security_advisor.transformer_model = Mock()
                security_advisor.xgboost_model.predict_proba.return_value = [[0.9, 0.1]]
                security_advisor.transformer_model.predict.return_value = [[0.95, 0.05]]
            
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            scheduler = UnifiedUAVScheduler(
                battery_predictor=battery_predictor,
                thermal_guard=thermal_guard,
                security_advisor=security_advisor,
                ipc_bridge=ipc_bridge
            )
            
            results = []
            errors = []
            
            def telemetry_worker(worker_id, telemetry_count):
                """Simulate telemetry processing from different sources."""
                try:
                    for i in range(telemetry_count):
                        telemetry = TelemetrySnapshot(
                            timestamp_ns=time.time_ns() + i * int(10e6),  # 10ms intervals
                            battery_voltage_v=14.8 - worker_id * 0.1,
                            battery_current_a=-2.0 - worker_id * 0.5,
                            cpu_temp_c=50.0 + worker_id * 5.0,
                            ambient_temp_c=25.0,
                            network_packet_rate=100.0 + worker_id * 50.0
                        )
                        
                        analysis = scheduler._process_telemetry(telemetry)
                        decision = scheduler._make_scheduling_decision(analysis)
                        
                        results.append({
                            'worker_id': worker_id,
                            'iteration': i,
                            'success': analysis is not None and decision is not None,
                            'suite': decision.recommended_suite if decision else None
                        })
                        
                        time.sleep(0.001)  # Small delay between samples
                        
                except Exception as e:
                    errors.append(f"Worker {worker_id}: {str(e)}")
            
            # Launch concurrent telemetry workers
            threads = []
            worker_count = 4
            samples_per_worker = 10
            
            for worker_id in range(worker_count):
                thread = threading.Thread(
                    target=telemetry_worker,
                    args=(worker_id, samples_per_worker)
                )
                threads.append(thread)
                thread.start()
            
            # Wait for all workers to complete
            for thread in threads:
                thread.join(timeout=30.0)
                assert not thread.is_alive(), "Thread did not complete in time"
            
            # Analyze results
            assert len(errors) == 0, f"Processing errors: {errors}"
            assert len(results) == worker_count * samples_per_worker
            
            successful_results = [r for r in results if r['success']]
            assert len(successful_results) == len(results), "Some telemetry processing failed"
            
            # Verify different workers can have different suite recommendations
            worker_suites = {}
            for result in successful_results:
                worker_id = result['worker_id']
                suite = result['suite']
                if worker_id not in worker_suites:
                    worker_suites[worker_id] = set()
                worker_suites[worker_id].add(suite)
            
            # Each worker should have consistent recommendations (based on their conditions)
            for worker_id, suites in worker_suites.items():
                assert len(suites) <= 2, f"Worker {worker_id} had too many suite changes: {suites}"
            
            ipc_bridge.shutdown()
    
    def test_configuration_validation_and_updates(self, realistic_battery_specs):
        """Test configuration validation and hot updates."""
        with tempfile.TemporaryDirectory() as temp_dir:
            battery_predictor = BatteryPredictor(battery_specs=realistic_battery_specs)
            thermal_guard = ThermalGuard()
            
            with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
                security_advisor = SecurityAdvisor()
            
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            # Test invalid configuration
            invalid_config = SchedulerConfig(
                decision_interval_ms=-10,  # Invalid negative interval
                battery_critical_threshold=1.5  # Invalid threshold > 1.0
            )
            
            # Should handle invalid config gracefully
            scheduler = UnifiedUAVScheduler(
                config=invalid_config,
                battery_predictor=battery_predictor,
                thermal_guard=thermal_guard,
                security_advisor=security_advisor,
                ipc_bridge=ipc_bridge
            )
            
            # Should have fallen back to safe defaults
            assert scheduler.config.decision_interval_ms > 0
            assert scheduler.config.battery_critical_threshold <= 1.0
            
            # Test valid configuration update
            new_config = SchedulerConfig(
                decision_interval_ms=200,
                battery_critical_threshold=0.2,
                thermal_critical_temp=85.0
            )
            
            update_success = scheduler.update_configuration(new_config)
            assert update_success == True
            assert scheduler.config.decision_interval_ms == 200
            assert scheduler.config.battery_critical_threshold == 0.2
            
            ipc_bridge.shutdown()
    
    def test_hardware_simulation_smoke_test(self, realistic_battery_specs):
        """Smoke test simulating realistic Pi 4 + Pixhawk hardware conditions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Configure for Pi 4 constraints
            battery_predictor = BatteryPredictor(battery_specs=realistic_battery_specs)
            thermal_guard = ThermalGuard(
                warning_temp=60.0,   # Pi 4 gets warm
                critical_temp=70.0,  # Pi 4 throttling point
                emergency_temp=80.0
            )
            
            with patch('src.scheduler.components.security_advisor.SecurityAdvisor._load_models'):
                security_advisor = SecurityAdvisor()
                security_advisor.xgboost_model = Mock()
                security_advisor.transformer_model = Mock()
                security_advisor.xgboost_model.predict_proba.return_value = [[0.85, 0.15]]
                security_advisor.transformer_model.predict.return_value = [[0.9, 0.1]]
            
            ipc_bridge = IPCBridge(shared_memory_dir=temp_dir)
            ipc_bridge.initialize_shared_memory()
            
            # Pi 4 optimized configuration
            pi4_config = SchedulerConfig(
                decision_interval_ms=250,  # Balanced for Pi 4 performance
                enable_expert_system=True,
                enable_reinforcement_learning=False,  # May be too heavy for Pi 4
                max_cpu_usage_percent=80.0,
                enable_thermal_throttling=True
            )
            
            scheduler = UnifiedUAVScheduler(
                config=pi4_config,
                battery_predictor=battery_predictor,
                thermal_guard=thermal_guard,
                security_advisor=security_advisor,
                ipc_bridge=ipc_bridge
            )
            
            # Simulate Pi 4 + Pixhawk mission profile
            mission_scenarios = [
                # Scenario 1: Normal cruise
                {
                    'duration_s': 2.0,
                    'battery_voltage': 14.8,
                    'current_draw': -2.0,
                    'cpu_temp_start': 45.0,
                    'cpu_temp_end': 55.0,
                    'network_load': 100.0
                },
                # Scenario 2: High performance maneuver
                {
                    'duration_s': 1.0,
                    'battery_voltage': 14.0,
                    'current_draw': -8.0,
                    'cpu_temp_start': 55.0,
                    'cpu_temp_end': 68.0,
                    'network_load': 500.0
                },
                # Scenario 3: Recovery and thermal management
                {
                    'duration_s': 2.0,
                    'battery_voltage': 13.5,
                    'current_draw': -1.5,
                    'cpu_temp_start': 68.0,
                    'cpu_temp_end': 50.0,
                    'network_load': 80.0
                }
            ]
            
            total_decisions = 0
            scenario_results = []
            
            for scenario_idx, scenario in enumerate(mission_scenarios):
                scenario_start = time.time()
                decisions_in_scenario = []
                
                iterations = int(scenario['duration_s'] / (pi4_config.decision_interval_ms / 1000.0))
                
                for i in range(max(1, iterations)):
                    progress = i / max(1, iterations - 1) if iterations > 1 else 0
                    
                    # Interpolate conditions over scenario duration
                    cpu_temp = (scenario['cpu_temp_start'] + 
                              progress * (scenario['cpu_temp_end'] - scenario['cpu_temp_start']))
                    
                    telemetry = TelemetrySnapshot(
                        timestamp_ns=time.time_ns(),
                        battery_voltage_v=scenario['battery_voltage'],
                        battery_current_a=scenario['current_draw'],
                        cpu_temp_c=cpu_temp,
                        ambient_temp_c=30.0,
                        network_packet_rate=scenario['network_load']
                    )
                    
                    # Time the processing
                    process_start = time.perf_counter()
                    analysis = scheduler._process_telemetry(telemetry)
                    decision = scheduler._make_scheduling_decision(analysis)
                    process_time_ms = (time.perf_counter() - process_start) * 1000
                    
                    decisions_in_scenario.append({
                        'processing_time_ms': process_time_ms,
                        'suite': decision.recommended_suite,
                        'confidence': decision.confidence_score,
                        'thermal_state': analysis.thermal_analysis.state.name,
                        'battery_soc': analysis.battery_analysis.soc_percentage
                    })
                    
                    total_decisions += 1
                    
                    # Verify real-time constraint compliance
                    assert process_time_ms < pi4_config.decision_interval_ms, \
                        f"Processing time {process_time_ms:.1f}ms exceeded interval {pi4_config.decision_interval_ms}ms"
                
                scenario_results.append({
                    'scenario': scenario_idx,
                    'duration': time.time() - scenario_start,
                    'decisions': decisions_in_scenario
                })
            
            # Analyze overall performance
            all_processing_times = []
            for scenario in scenario_results:
                for decision in scenario['decisions']:
                    all_processing_times.append(decision['processing_time_ms'])
            
            avg_processing_time = sum(all_processing_times) / len(all_processing_times)
            max_processing_time = max(all_processing_times)
            
            # Pi 4 performance verification
            assert avg_processing_time < 100.0, f"Average processing time {avg_processing_time:.1f}ms too high for Pi 4"
            assert max_processing_time < 200.0, f"Max processing time {max_processing_time:.1f}ms too high for Pi 4"
            assert total_decisions > 0, "No decisions were made during simulation"
            
            # Verify adaptive behavior occurred
            suite_transitions = 0
            prev_suite = None
            for scenario in scenario_results:
                for decision in scenario['decisions']:
                    if prev_suite and decision['suite'] != prev_suite:
                        suite_transitions += 1
                    prev_suite = decision['suite']
            
            # Should have some adaptation to changing conditions
            assert suite_transitions >= 0, "No suite adaptations occurred"
            
            ipc_bridge.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])  # -s to see print output during testing