#!/usr/bin/env python3
"""
Comprehensive test runner for the UAV scheduler test suite.
Provides organized test execution with performance reporting.
"""

import pytest
import sys
import time
import json
import os
from pathlib import Path


def run_unit_tests():
    """Run unit tests for individual components."""
    print("🔬 Running Unit Tests...")
    print("=" * 60)
    
    unit_test_files = [
        "src/scheduler/components/tests/test_battery_predictor.py",
        "src/scheduler/components/tests/test_thermal_guard.py", 
        "src/scheduler/components/tests/test_security_advisor.py",
        "src/scheduler/components/tests/test_ipc_bridge.py",
        "src/scheduler/components/tests/test_unified_scheduler.py"
    ]
    
    results = {}
    
    for test_file in unit_test_files:
        if os.path.exists(test_file):
            component_name = Path(test_file).stem.replace("test_", "")
            print(f"\n📋 Testing {component_name}...")
            
            start_time = time.time()
            result = pytest.main([
                test_file,
                "-v", 
                "--tb=short",
                "--disable-warnings",
                "-x"  # Stop on first failure
            ])
            execution_time = time.time() - start_time
            
            results[component_name] = {
                "status": "PASSED" if result == 0 else "FAILED",
                "execution_time": execution_time,
                "return_code": result
            }
            
            if result == 0:
                print(f"✅ {component_name} tests passed ({execution_time:.2f}s)")
            else:
                print(f"❌ {component_name} tests failed ({execution_time:.2f}s)")
                return False, results
        else:
            print(f"⚠️  Test file not found: {test_file}")
    
    return True, results


def run_integration_tests():
    """Run integration tests for the complete system."""
    print("\n🔗 Running Integration Tests...")
    print("=" * 60)
    
    start_time = time.time()
    result = pytest.main([
        "src/scheduler/components/tests/test_integration.py",
        "-v",
        "--tb=short", 
        "--disable-warnings",
        "-m", "not slow"  # Skip slow tests by default
    ])
    execution_time = time.time() - start_time
    
    if result == 0:
        print(f"✅ Integration tests passed ({execution_time:.2f}s)")
        return True, execution_time
    else:
        print(f"❌ Integration tests failed ({execution_time:.2f}s)")
        return False, execution_time


def run_performance_tests():
    """Run performance benchmark tests."""
    print("\n⚡ Running Performance Tests...")
    print("=" * 60)
    
    start_time = time.time()
    result = pytest.main([
        "src/scheduler/components/tests/",
        "-v",
        "--tb=short",
        "--disable-warnings", 
        "-m", "performance",
        "--durations=10"  # Show 10 slowest tests
    ])
    execution_time = time.time() - start_time
    
    if result == 0:
        print(f"✅ Performance tests passed ({execution_time:.2f}s)")
        return True, execution_time
    else:
        print(f"❌ Performance tests failed ({execution_time:.2f}s)")
        return False, execution_time


def run_hardware_simulation_tests():
    """Run hardware simulation tests (Pi 4 + Pixhawk)."""
    print("\n🖥️  Running Hardware Simulation Tests...")
    print("=" * 60)
    
    start_time = time.time()
    result = pytest.main([
        "src/scheduler/components/tests/",
        "-v",
        "--tb=short",
        "--disable-warnings",
        "-m", "hardware",
        "-s"  # Show output for hardware tests
    ])
    execution_time = time.time() - start_time
    
    if result == 0:
        print(f"✅ Hardware simulation tests passed ({execution_time:.2f}s)")
        return True, execution_time
    else:
        print(f"❌ Hardware simulation tests failed ({execution_time:.2f}s)")
        return False, execution_time


def run_all_tests():
    """Run the complete test suite with comprehensive reporting."""
    print("🚀 UAV Scheduler Test Suite")
    print("=" * 60)
    print("Testing battery-aware, thermal-aware, security-adaptive PQC scheduler")
    print("Target: Raspberry Pi 4 + Pixhawk UAV systems")
    print("=" * 60)
    
    overall_start = time.time()
    test_results = {
        "timestamp": time.time(),
        "total_duration": 0,
        "unit_tests": {},
        "integration_tests": {},
        "performance_tests": {},
        "hardware_simulation": {},
        "overall_status": "UNKNOWN"
    }
    
    # Run unit tests
    unit_success, unit_results = run_unit_tests()
    test_results["unit_tests"] = unit_results
    
    if not unit_success:
        print("\n❌ Unit tests failed - stopping test execution")
        test_results["overall_status"] = "FAILED_UNIT_TESTS"
        return test_results
    
    # Run integration tests
    integration_success, integration_time = run_integration_tests()
    test_results["integration_tests"] = {
        "status": "PASSED" if integration_success else "FAILED",
        "execution_time": integration_time
    }
    
    if not integration_success:
        print("\n❌ Integration tests failed - continuing with remaining tests")
    
    # Run performance tests
    performance_success, performance_time = run_performance_tests()
    test_results["performance_tests"] = {
        "status": "PASSED" if performance_success else "FAILED", 
        "execution_time": performance_time
    }
    
    # Run hardware simulation tests
    hardware_success, hardware_time = run_hardware_simulation_tests()
    test_results["hardware_simulation"] = {
        "status": "PASSED" if hardware_success else "FAILED",
        "execution_time": hardware_time
    }
    
    # Calculate overall results
    overall_time = time.time() - overall_start
    test_results["total_duration"] = overall_time
    
    all_passed = (unit_success and integration_success and 
                  performance_success and hardware_success)
    test_results["overall_status"] = "PASSED" if all_passed else "PARTIAL_FAILURE"
    
    # Print summary
    print("\n" + "=" * 60)
    print("📊 TEST SUITE SUMMARY")
    print("=" * 60)
    
    print(f"⏱️  Total execution time: {overall_time:.2f} seconds")
    print(f"🔬 Unit tests: {'✅ PASSED' if unit_success else '❌ FAILED'}")
    print(f"🔗 Integration tests: {'✅ PASSED' if integration_success else '❌ FAILED'}")
    print(f"⚡ Performance tests: {'✅ PASSED' if performance_success else '❌ FAILED'}")
    print(f"🖥️  Hardware simulation: {'✅ PASSED' if hardware_success else '❌ FAILED'}")
    
    if all_passed:
        print("\n🎉 ALL TESTS PASSED - Scheduler ready for deployment!")
    else:
        print("\n⚠️  SOME TESTS FAILED - Review failures before deployment")
    
    # Save detailed results
    results_file = "test_results.json"
    with open(results_file, 'w') as f:
        json.dump(test_results, f, indent=2)
    print(f"\n📄 Detailed results saved to: {results_file}")
    
    return test_results


def run_quick_tests():
    """Run a quick subset of tests for rapid development feedback."""
    print("🏃 Quick Test Suite (Development Mode)")
    print("=" * 60)
    
    start_time = time.time()
    result = pytest.main([
        "src/scheduler/components/tests/",
        "-v",
        "--tb=short",
        "--disable-warnings",
        "-x",  # Stop on first failure
        "-m", "not slow and not hardware",  # Skip slow and hardware tests
        "--maxfail=3"  # Stop after 3 failures
    ])
    execution_time = time.time() - start_time
    
    if result == 0:
        print(f"\n✅ Quick tests passed ({execution_time:.2f}s)")
        print("🚀 Ready for development iteration!")
    else:
        print(f"\n❌ Quick tests failed ({execution_time:.2f}s)")
        print("🔧 Fix issues before continuing development")
    
    return result == 0


def run_coverage_analysis():
    """Run tests with coverage analysis."""
    print("📈 Running Test Coverage Analysis...")
    print("=" * 60)
    
    try:
        result = pytest.main([
            "src/scheduler/components/tests/",
            "--cov=src/scheduler/",
            "--cov-report=html:htmlcov",
            "--cov-report=term-missing",
            "--cov-fail-under=80",  # Require 80% coverage
            "-v"
        ])
        
        if result == 0:
            print("\n✅ Coverage analysis completed")
            print("📁 HTML coverage report: htmlcov/index.html")
        else:
            print("\n❌ Coverage analysis failed or insufficient coverage")
        
        return result == 0
        
    except ImportError:
        print("⚠️  pytest-cov not installed. Install with: pip install pytest-cov")
        return False


def main():
    """Main test runner with command line options."""
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        
        if mode == "quick":
            success = run_quick_tests()
            sys.exit(0 if success else 1)
            
        elif mode == "unit":
            success, _ = run_unit_tests()
            sys.exit(0 if success else 1)
            
        elif mode == "integration":
            success, _ = run_integration_tests()
            sys.exit(0 if success else 1)
            
        elif mode == "performance":
            success, _ = run_performance_tests()
            sys.exit(0 if success else 1)
            
        elif mode == "hardware":
            success, _ = run_hardware_simulation_tests()
            sys.exit(0 if success else 1)
            
        elif mode == "coverage":
            success = run_coverage_analysis()
            sys.exit(0 if success else 1)
            
        else:
            print(f"Unknown test mode: {mode}")
            print("Available modes: quick, unit, integration, performance, hardware, coverage")
            sys.exit(1)
    
    else:
        # Run full test suite
        results = run_all_tests()
        success = results["overall_status"] == "PASSED"
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    # Ensure we're in the right directory
    os.chdir(Path(__file__).parent.parent.parent.parent)
    main()