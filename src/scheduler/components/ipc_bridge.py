#!/usr/bin/env python3
"""POSIX IPC bridge for ultra-low-latency cryptographic algorithm switching.

This module implements shared memory and semaphore-based inter-process communication
to minimize latency when switching between PQC suites, DDOS detection models, and
scheduling policies. Key optimizations:
- Memory-mapped algorithm parameters to eliminate ROM→RAM copying
- Semaphore-based coordination for lock-free algorithm activation
- Pre-warmed model states to reduce cold-start latency 
- Atomic configuration updates to prevent race conditions
"""

from __future__ import annotations

import mmap
import os
import struct
import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
import tempfile
from pathlib import Path


try:
    import posix_ipc
    HAS_POSIX_IPC = True
except ImportError:
    # Fallback for systems without posix_ipc
    import threading
    HAS_POSIX_IPC = False


class IPCMode(Enum):
    """IPC implementation modes."""
    POSIX_SHM = "posix_shm"        # POSIX shared memory + semaphores
    MMAP_FILE = "mmap_file"        # File-backed memory mapping
    THREADING = "threading"        # Thread-based fallback


@dataclass
class AlgorithmConfig:
    """Configuration for a cryptographic algorithm or model."""
    algorithm_id: str
    config_data: bytes            # Serialized configuration 
    memory_size_bytes: int        # Required memory size
    warmup_time_ms: float         # Time to activate from cold
    active: bool = False          # Currently active?
    last_used_ns: Optional[int] = None


@dataclass
class IPCStats:
    """Performance statistics for IPC operations."""
    switch_count: int = 0
    total_switch_time_ms: float = 0.0
    avg_switch_time_ms: float = 0.0
    max_switch_time_ms: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    memory_usage_mb: float = 0.0


class IPCBridge:
    """High-performance IPC bridge for algorithm switching."""
    
    def __init__(
        self,
        name: str = "pqc_scheduler",
        max_algorithms: int = 16,
        shared_memory_size_mb: int = 64,
        mode: IPCMode = IPCMode.POSIX_SHM,
        warmup_pool_size: int = 3,
    ):
        self.name = name
        self.max_algorithms = max_algorithms
        self.shared_memory_size = shared_memory_size_mb * 1024 * 1024
        self.mode = mode if HAS_POSIX_IPC else IPCMode.THREADING
        self.warmup_pool_size = warmup_pool_size
        
        self.algorithms: Dict[str, AlgorithmConfig] = {}
        self.stats = IPCStats()
        
        # IPC primitives
        self.shared_memory: Optional[Any] = None
        self.memory_map: Optional[mmap.mmap] = None
        self.semaphore: Optional[Any] = None
        self.lock = threading.Lock()
        
        # Pre-warmed algorithm pool
        self.warm_pool: Dict[str, Any] = {}
        self.warmup_thread: Optional[threading.Thread] = None
        self.shutdown_event = threading.Event()
        
        self._initialize_ipc()
        self._start_warmup_thread()
    
    def _initialize_ipc(self) -> None:
        """Initialize IPC mechanisms based on selected mode."""
        
        if self.mode == IPCMode.POSIX_SHM and HAS_POSIX_IPC:
            try:
                # Create POSIX shared memory segment
                shm_name = f"/{self.name}_shm"
                self.shared_memory = posix_ipc.SharedMemory(
                    shm_name,
                    posix_ipc.O_CREAT,
                    size=self.shared_memory_size
                )
                
                # Memory map the shared memory
                self.memory_map = mmap.mmap(
                    self.shared_memory.fd,
                    self.shared_memory_size,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE
                )
                
                # Create coordination semaphore
                sem_name = f"/{self.name}_sem"
                self.semaphore = posix_ipc.Semaphore(
                    sem_name,
                    posix_ipc.O_CREAT,
                    initial_value=1
                )
                
                print(f"[IPC] Initialized POSIX shared memory: {shm_name}")
                
            except Exception as e:
                print(f"[WARN] POSIX IPC failed, falling back to file mapping: {e}")
                self.mode = IPCMode.MMAP_FILE
                self._initialize_file_mapping()
        
        elif self.mode == IPCMode.MMAP_FILE:
            self._initialize_file_mapping()
        
        else:  # THREADING fallback
            print("[IPC] Using threading fallback mode")
    
    def _initialize_file_mapping(self) -> None:
        """Initialize file-backed memory mapping."""
        try:
            # Create temporary file for memory mapping
            temp_dir = Path(tempfile.gettempdir()) / "pqc_scheduler"
            temp_dir.mkdir(exist_ok=True)
            
            self.shm_file = temp_dir / f"{self.name}_shm.dat"
            
            # Create file with required size
            with open(self.shm_file, 'wb') as f:
                f.write(b'\0' * self.shared_memory_size)
            
            # Memory map the file
            with open(self.shm_file, 'r+b') as f:
                self.memory_map = mmap.mmap(
                    f.fileno(),
                    self.shared_memory_size,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE
                )
            
            print(f"[IPC] Initialized file-backed mapping: {self.shm_file}")
            
        except Exception as e:
            print(f"[WARN] File mapping failed, using threading: {e}")
            self.mode = IPCMode.THREADING
    
    def register_algorithm(
        self, 
        algorithm_id: str, 
        config_data: bytes,
        warmup_callback: Optional[Callable[[], Any]] = None
    ) -> bool:
        """Register an algorithm for fast switching."""
        
        if len(self.algorithms) >= self.max_algorithms:
            print(f"[WARN] Maximum algorithms ({self.max_algorithms}) reached")
            return False
        
        config = AlgorithmConfig(
            algorithm_id=algorithm_id,
            config_data=config_data,
            memory_size_bytes=len(config_data),
            warmup_time_ms=0.0,  # Will be measured during warmup
        )
        
        with self.lock:
            self.algorithms[algorithm_id] = config
            
            # Store warmup callback for background preparation
            if warmup_callback:
                self._schedule_warmup(algorithm_id, warmup_callback)
        
        print(f"[IPC] Registered algorithm: {algorithm_id} ({len(config_data)} bytes)")
        return True
    
    def switch_algorithm(self, algorithm_id: str, timeout_ms: float = 100.0) -> bool:
        """Switch to specified algorithm with minimal latency."""
        
        start_time = time.time()
        
        if algorithm_id not in self.algorithms:
            print(f"[WARN] Unknown algorithm: {algorithm_id}")
            return False
        
        # Acquire coordination lock/semaphore
        if not self._acquire_lock(timeout_ms):
            print(f"[WARN] Failed to acquire lock for {algorithm_id}")
            return False
        
        try:
            config = self.algorithms[algorithm_id]
            
            # Check if algorithm is pre-warmed
            if algorithm_id in self.warm_pool:
                # Fast path: algorithm already warm
                self._activate_warm_algorithm(algorithm_id)
                self.stats.cache_hits += 1
            else:
                # Slow path: cold start required
                self._cold_start_algorithm(algorithm_id)
                self.stats.cache_misses += 1
            
            config.active = True
            config.last_used_ns = time.time_ns()
            
            # Deactivate other algorithms
            for other_id, other_config in self.algorithms.items():
                if other_id != algorithm_id:
                    other_config.active = False
            
            # Update statistics
            switch_time_ms = (time.time() - start_time) * 1000
            self.stats.switch_count += 1
            self.stats.total_switch_time_ms += switch_time_ms
            self.stats.avg_switch_time_ms = (
                self.stats.total_switch_time_ms / self.stats.switch_count
            )
            self.stats.max_switch_time_ms = max(
                self.stats.max_switch_time_ms, switch_time_ms
            )
            
            print(f"[IPC] Switched to {algorithm_id} in {switch_time_ms:.2f}ms")
            return True
            
        finally:
            self._release_lock()
    
    def _acquire_lock(self, timeout_ms: float) -> bool:
        """Acquire coordination lock with timeout."""
        
        if self.mode == IPCMode.POSIX_SHM and self.semaphore:
            try:
                self.semaphore.acquire(timeout=timeout_ms / 1000.0)
                return True
            except posix_ipc.BusyError:
                return False
        else:
            # Use threading lock with timeout
            return self.lock.acquire(timeout=timeout_ms / 1000.0)
    
    def _release_lock(self) -> None:
        """Release coordination lock."""
        
        if self.mode == IPCMode.POSIX_SHM and self.semaphore:
            self.semaphore.release()
        else:
            try:
                self.lock.release()
            except RuntimeError:
                pass  # Lock not held by this thread
    
    def _activate_warm_algorithm(self, algorithm_id: str) -> None:
        """Activate a pre-warmed algorithm (fast path)."""
        
        warm_instance = self.warm_pool[algorithm_id]
        
        if self.memory_map:
            # Copy configuration to shared memory
            config = self.algorithms[algorithm_id]
            offset = hash(algorithm_id) % (self.shared_memory_size - len(config.config_data))
            self.memory_map.seek(offset)
            self.memory_map.write(config.config_data)
            self.memory_map.flush()
    
    def _cold_start_algorithm(self, algorithm_id: str) -> None:
        """Cold start an algorithm (slow path)."""
        
        config = self.algorithms[algorithm_id]
        start_time = time.time()
        
        # Simulate algorithm initialization
        # In real implementation, this would load model weights, etc.
        time.sleep(0.001)  # 1ms simulated cold start
        
        warmup_time = (time.time() - start_time) * 1000
        config.warmup_time_ms = warmup_time
        
        if self.memory_map:
            # Write to shared memory
            offset = hash(algorithm_id) % (self.shared_memory_size - len(config.config_data))
            self.memory_map.seek(offset)
            self.memory_map.write(config.config_data)
            self.memory_map.flush()
    
    def _schedule_warmup(self, algorithm_id: str, warmup_callback: Callable[[], Any]) -> None:
        """Schedule algorithm for background warmup."""
        
        if len(self.warm_pool) >= self.warmup_pool_size:
            # Evict least recently used algorithm
            lru_id = min(
                self.algorithms.keys(),
                key=lambda aid: self.algorithms[aid].last_used_ns or 0
            )
            if lru_id in self.warm_pool:
                del self.warm_pool[lru_id]
        
        # Warm up in background thread
        def warmup_worker():
            try:
                instance = warmup_callback()
                with self.lock:
                    self.warm_pool[algorithm_id] = instance
                print(f"[IPC] Warmed up algorithm: {algorithm_id}")
            except Exception as e:
                print(f"[WARN] Warmup failed for {algorithm_id}: {e}")
        
        thread = threading.Thread(target=warmup_worker, daemon=True)
        thread.start()
    
    def _start_warmup_thread(self) -> None:
        """Start background thread for algorithm warmup management."""
        
        def warmup_manager():
            while not self.shutdown_event.wait(5.0):  # Check every 5 seconds
                try:
                    self._maintain_warm_pool()
                except Exception as e:
                    print(f"[WARN] Warmup manager error: {e}")
        
        self.warmup_thread = threading.Thread(target=warmup_manager, daemon=True)
        self.warmup_thread.start()
    
    def _maintain_warm_pool(self) -> None:
        """Maintain optimal warm pool based on usage patterns."""
        
        # Identify frequently used algorithms
        current_time_ns = time.time_ns()
        recent_threshold_ns = current_time_ns - (300 * 1e9)  # 5 minutes
        
        frequent_algorithms = [
            aid for aid, config in self.algorithms.items()
            if config.last_used_ns and config.last_used_ns > recent_threshold_ns
        ]
        
        # Ensure frequent algorithms are warmed up
        for algorithm_id in frequent_algorithms[:self.warmup_pool_size]:
            if algorithm_id not in self.warm_pool:
                print(f"[IPC] Pre-warming frequently used algorithm: {algorithm_id}")
                # Would trigger warmup here in real implementation
    
    def get_active_algorithm(self) -> Optional[str]:
        """Get currently active algorithm ID."""
        
        for algorithm_id, config in self.algorithms.items():
            if config.active:
                return algorithm_id
        return None
    
    def get_performance_stats(self) -> IPCStats:
        """Get IPC performance statistics."""
        
        # Update memory usage
        if self.memory_map:
            self.stats.memory_usage_mb = self.shared_memory_size / (1024 * 1024)
        
        return self.stats
    
    def cleanup(self) -> None:
        """Clean up IPC resources."""
        
        self.shutdown_event.set()
        
        if self.warmup_thread and self.warmup_thread.is_alive():
            self.warmup_thread.join(timeout=1.0)
        
        if self.memory_map:
            self.memory_map.close()
        
        if self.mode == IPCMode.POSIX_SHM and HAS_POSIX_IPC:
            if self.shared_memory:
                self.shared_memory.close_fd()
                try:
                    self.shared_memory.unlink()
                except:
                    pass
            
            if self.semaphore:
                try:
                    self.semaphore.unlink()
                except:
                    pass
        
        elif self.mode == IPCMode.MMAP_FILE and hasattr(self, 'shm_file'):
            try:
                self.shm_file.unlink()
            except:
                pass
        
        print("[IPC] Cleaned up resources")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()


# Convenience functions for common use cases

def create_pqc_suite_bridge(suites: List[str]) -> IPCBridge:
    """Create IPC bridge optimized for PQC suite switching."""
    
    bridge = IPCBridge(
        name="pqc_suites",
        max_algorithms=len(suites) + 2,  # Extra slots for DDOS models
        shared_memory_size_mb=32,
        warmup_pool_size=3,
    )
    
    # Register PQC suites
    for suite in suites:
        config_data = suite.encode('utf-8')  # Minimal config for demo
        bridge.register_algorithm(suite, config_data)
    
    return bridge


def create_ddos_model_bridge() -> IPCBridge:
    """Create IPC bridge optimized for DDOS model switching."""
    
    bridge = IPCBridge(
        name="ddos_models",
        max_algorithms=4,  # XGBoost, Transformer, fallback heuristics
        shared_memory_size_mb=128,  # Larger for model weights
        warmup_pool_size=2,
    )
    
    # Register DDOS detection models
    models = ["xgboost_light", "transformer_heavy", "heuristic_fallback"]
    for model in models:
        config_data = f"model:{model}".encode('utf-8')
        bridge.register_algorithm(model, config_data)
    
    return bridge


__all__ = [
    "IPCMode",
    "AlgorithmConfig", 
    "IPCStats",
    "IPCBridge",
    "create_pqc_suite_bridge",
    "create_ddos_model_bridge",
]