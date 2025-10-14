#!/usr/bin/env python3
"""Unit tests for IPC bridge component."""

import pytest
import time
import threading
import tempfile
import os
from unittest.mock import Mock, patch
from src.scheduler.components.ipc_bridge import (
    IPCBridge, AlgorithmType, IPCMessage, SharedMemoryConfig
)


class TestIPCBridge:
    
    def test_ipc_bridge_initialization(self):
        """Test IPC bridge initialization with default parameters."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            
            assert bridge.shared_memory_size == 4096
            assert bridge.current_algorithm == AlgorithmType.EXPERT_SYSTEM
            assert bridge.algorithm_warm_pool_size == 3
            assert bridge.shared_memory_dir == temp_dir
    
    def test_shared_memory_initialization(self):
        """Test shared memory segment creation and mapping."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            
            success = bridge.initialize_shared_memory()
            assert success == True
            
            # Verify shared memory file exists
            shm_path = os.path.join(temp_dir, "scheduler_ipc")
            assert os.path.exists(shm_path)
            
            # Verify size
            assert os.path.getsize(shm_path) == bridge.shared_memory_size
    
    def test_algorithm_switching_basic(self):
        """Test basic algorithm switching functionality."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Switch to RL algorithm
            switch_time = bridge.switch_algorithm(
                target_algorithm=AlgorithmType.REINFORCEMENT_LEARNING,
                priority_ms=10
            )
            
            assert switch_time > 0
            assert bridge.current_algorithm == AlgorithmType.REINFORCEMENT_LEARNING
    
    def test_algorithm_prewarming(self):
        """Test algorithm prewarming for faster switching."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir, algorithm_warm_pool_size=2)
            bridge.initialize_shared_memory()
            
            # Prewarm algorithms
            bridge.prewarm_algorithms([
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION
            ])
            
            # Verify warm pool
            assert AlgorithmType.REINFORCEMENT_LEARNING in bridge.warm_algorithm_pool
            assert AlgorithmType.HYBRID_FUSION in bridge.warm_algorithm_pool
            
            # Switch to prewarmed algorithm should be faster
            warm_switch_time = bridge.switch_algorithm(
                target_algorithm=AlgorithmType.REINFORCEMENT_LEARNING,
                priority_ms=5
            )
            
            # Should be very fast due to prewarming
            assert warm_switch_time < 5.0  # milliseconds
    
    def test_concurrent_algorithm_switching(self):
        """Test concurrent algorithm switching from multiple threads."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            switch_results = []
            
            def switch_worker(target_algorithm, thread_id):
                try:
                    switch_time = bridge.switch_algorithm(
                        target_algorithm=target_algorithm,
                        priority_ms=thread_id
                    )
                    switch_results.append((thread_id, switch_time, True))
                except Exception as e:
                    switch_results.append((thread_id, 0, False))
            
            # Launch concurrent switches
            threads = []
            algorithms = [
                AlgorithmType.EXPERT_SYSTEM,
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION
            ]
            
            for i, alg in enumerate(algorithms):
                thread = threading.Thread(
                    target=switch_worker,
                    args=(alg, i + 1)
                )
                threads.append(thread)
                thread.start()
            
            # Wait for completion
            for thread in threads:
                thread.join(timeout=5.0)
            
            # Verify all switches completed
            assert len(switch_results) == 3
            successful_switches = [r for r in switch_results if r[2]]
            assert len(successful_switches) > 0
    
    def test_message_passing_basic(self):
        """Test basic IPC message passing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Send message
            message = IPCMessage(
                sender_id="test_sender",
                message_type="config_update",
                payload={"battery_soc": 0.75, "thermal_state": "normal"},
                priority=5
            )
            
            success = bridge.send_message(message)
            assert success == True
            
            # Receive message
            received = bridge.receive_message(timeout_ms=100)
            assert received is not None
            assert received.sender_id == "test_sender"
            assert received.message_type == "config_update"
            assert received.payload["battery_soc"] == 0.75
    
    def test_message_queue_overflow(self):
        """Test message queue behavior under overflow conditions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir, max_message_queue_size=3)
            bridge.initialize_shared_memory()
            
            # Fill message queue beyond capacity
            messages_sent = 0
            for i in range(5):
                message = IPCMessage(
                    sender_id=f"sender_{i}",
                    message_type="test",
                    payload={"index": i},
                    priority=i
                )
                if bridge.send_message(message):
                    messages_sent += 1
            
            # Should have dropped lower priority messages
            assert messages_sent <= 3
            
            # Receive all available messages
            received_messages = []
            while True:
                msg = bridge.receive_message(timeout_ms=10)
                if msg is None:
                    break
                received_messages.append(msg)
            
            # Should receive highest priority messages
            assert len(received_messages) <= 3
            if len(received_messages) > 0:
                priorities = [msg.priority for msg in received_messages]
                assert max(priorities) >= 2  # Higher priority messages preserved
    
    def test_performance_metrics_collection(self):
        """Test collection of IPC performance metrics."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Perform several operations
            for i in range(5):
                bridge.switch_algorithm(
                    target_algorithm=AlgorithmType.REINFORCEMENT_LEARNING,
                    priority_ms=1
                )
                bridge.switch_algorithm(
                    target_algorithm=AlgorithmType.EXPERT_SYSTEM,
                    priority_ms=1
                )
            
            metrics = bridge.get_performance_metrics()
            
            assert "switch_count" in metrics
            assert "avg_switch_time_ms" in metrics
            assert "total_messages_sent" in metrics
            assert "total_messages_received" in metrics
            
            assert metrics["switch_count"] >= 10
            assert metrics["avg_switch_time_ms"] > 0
    
    def test_algorithm_state_persistence(self):
        """Test persistence of algorithm state across switches."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Set initial state for expert system
            initial_state = {
                "rule_weights": [0.5, 0.3, 0.2],
                "threshold_values": {"battery": 0.3, "thermal": 75.0}
            }
            
            bridge.set_algorithm_state(AlgorithmType.EXPERT_SYSTEM, initial_state)
            
            # Switch to different algorithm
            bridge.switch_algorithm(AlgorithmType.REINFORCEMENT_LEARNING)
            
            # Switch back and verify state persistence
            bridge.switch_algorithm(AlgorithmType.EXPERT_SYSTEM)
            restored_state = bridge.get_algorithm_state(AlgorithmType.EXPERT_SYSTEM)
            
            assert restored_state == initial_state
    
    def test_memory_mapped_config_updates(self):
        """Test memory-mapped configuration updates."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Update configuration
            config_updates = {
                "battery_critical_threshold": 0.15,
                "thermal_warning_temp": 70.0,
                "security_threat_threshold": 0.8
            }
            
            success = bridge.update_shared_config(config_updates)
            assert success == True
            
            # Read back configuration
            current_config = bridge.get_shared_config()
            
            for key, value in config_updates.items():
                assert current_config[key] == value
    
    def test_semaphore_coordination(self):
        """Test semaphore-based coordination between processes."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            coordination_results = []
            
            def coordinated_worker(worker_id, iterations):
                for _ in range(iterations):
                    acquired = bridge.acquire_coordination_semaphore(timeout_ms=100)
                    if acquired:
                        # Critical section
                        time.sleep(0.001)  # Simulate work
                        bridge.release_coordination_semaphore()
                        coordination_results.append(worker_id)
            
            # Launch coordinated workers
            threads = []
            for i in range(3):
                thread = threading.Thread(
                    target=coordinated_worker,
                    args=(i, 5)
                )
                threads.append(thread)
                thread.start()
            
            # Wait for completion
            for thread in threads:
                thread.join(timeout=5.0)
            
            # Verify coordination worked (all workers made progress)
            assert len(coordination_results) == 15  # 3 workers × 5 iterations
            assert len(set(coordination_results)) == 3  # All workers participated
    
    def test_ipc_cleanup_and_shutdown(self):
        """Test proper cleanup and shutdown of IPC resources."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            shm_path = os.path.join(temp_dir, "scheduler_ipc")
            assert os.path.exists(shm_path)
            
            # Shutdown and cleanup
            bridge.shutdown()
            
            # Verify cleanup (shared memory file should be removed)
            assert not os.path.exists(shm_path)
    
    def test_error_handling_and_recovery(self):
        """Test error handling and recovery mechanisms."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            
            # Test operation without initialization
            success = bridge.switch_algorithm(AlgorithmType.REINFORCEMENT_LEARNING)
            assert success == False
            
            # Initialize and test invalid algorithm
            bridge.initialize_shared_memory()
            
            # Test with invalid shared memory access
            bridge.shared_memory_fd = -1  # Simulate corruption
            
            success = bridge.send_message(IPCMessage(
                sender_id="test",
                message_type="test",
                payload={},
                priority=1
            ))
            assert success == False
    
    def test_algorithm_switching_latency(self):
        """Test algorithm switching latency under various conditions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bridge = IPCBridge(shared_memory_dir=temp_dir)
            bridge.initialize_shared_memory()
            
            # Prewarm algorithms for fair comparison
            bridge.prewarm_algorithms([
                AlgorithmType.EXPERT_SYSTEM,
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION
            ])
            
            latencies = []
            
            # Measure switching latencies
            algorithms = [
                AlgorithmType.EXPERT_SYSTEM,
                AlgorithmType.REINFORCEMENT_LEARNING,
                AlgorithmType.HYBRID_FUSION
            ]
            
            for i in range(10):
                target_alg = algorithms[i % len(algorithms)]
                switch_time = bridge.switch_algorithm(target_alg, priority_ms=1)
                latencies.append(switch_time)
            
            # Verify sub-millisecond switching for prewarmed algorithms
            avg_latency = sum(latencies) / len(latencies)
            assert avg_latency < 1.0  # Should be under 1ms on average
            
            # Verify consistency (low variance)
            max_latency = max(latencies)
            min_latency = min(latencies)
            assert (max_latency - min_latency) < 2.0  # Low variance


if __name__ == "__main__":
    pytest.main([__file__, "-v"])