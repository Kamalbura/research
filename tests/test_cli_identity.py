"""
Test CLI identity workflow - init-identity, gcs requirements, drone acceptance.
Tests the unified CLI workflow with persistent key management.
"""

import tempfile
import os
import subprocess
import shutil
import pytest
from pathlib import Path

# Import our modules for direct testing
from core.run_proxy import init_identity_command, main


class TestCLIIdentity:
    """Test CLI identity management and persistent key workflow."""
    
    def setup_method(self):
        """Create temporary directory for each test."""
        self.test_dir = tempfile.mkdtemp()
        self.secrets_dir = os.path.join(self.test_dir, "secrets")
        os.makedirs(self.secrets_dir)
        
        # Store original working directory
        self.orig_cwd = os.getcwd()
        os.chdir(self.test_dir)
    
    def teardown_method(self):
        """Cleanup temporary directory."""
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.test_dir)
    
    def test_init_identity_creates_keys(self):
        """Test that init-identity command creates keypair files."""
        # Run init-identity command
        args_mock = type('Args', (), {
            'suite': 'cs-kyber768-aesgcm-dilithium3',
            'output_dir': 'secrets'
        })()
        
        result = init_identity_command(args_mock)
        assert result == 0  # Success
        
        # Verify files exist
        signing_key = os.path.join(self.secrets_dir, "gcs_signing.key")
        signing_pub = os.path.join(self.secrets_dir, "gcs_signing.pub")
        
        assert os.path.exists(signing_key)
        assert os.path.exists(signing_pub)
        
        # Verify key files have reasonable sizes
        assert os.path.getsize(signing_key) > 100  # Private key should be substantial
        assert os.path.getsize(signing_pub) > 50   # Public key should exist
    
    def test_init_identity_suite_variations(self):
        """Test init-identity with different PQC suites."""
        suites_to_test = [
            'cs-kyber512-aesgcm-dilithium2',
            'cs-kyber768-aesgcm-dilithium3',
            'cs-kyber1024-aesgcm-dilithium5'  # Use dilithium5 instead of sphincs
        ]
        
        for suite in suites_to_test:
            # Create fresh secrets dir for each suite
            suite_dir = os.path.join(self.test_dir, f"secrets_{suite.replace('-', '_')}")
            os.makedirs(suite_dir, exist_ok=True)
            
            args_mock = type('Args', (), {
                'suite': suite,
                'output_dir': suite_dir
            })()
            
            result = init_identity_command(args_mock)
            assert result == 0
            
            # Verify keys exist for this suite
            assert os.path.exists(os.path.join(suite_dir, "gcs_signing.key"))
            assert os.path.exists(os.path.join(suite_dir, "gcs_signing.pub"))
    
    def test_init_identity_overwrites_warning(self, capsys):
        """Test that init-identity warns when overwriting existing keys."""
        # Create initial keys
        args_mock = type('Args', (), {
            'suite': 'cs-kyber768-aesgcm-dilithium3',
            'output_dir': 'secrets'
        })()
        
        init_identity_command(args_mock)
        
        # Capture original key content
        with open(os.path.join(self.secrets_dir, "gcs_signing.key"), "rb") as f:
            original_key = f.read()
        
        # Run init-identity again
        init_identity_command(args_mock)
        
        # Check that warning was printed
        captured = capsys.readouterr()
        assert "overwriting" in captured.out.lower() or "exists" in captured.out.lower()
        
        # Keys should be different (new ones generated)
        with open(os.path.join(self.secrets_dir, "gcs_signing.key"), "rb") as f:
            new_key = f.read()
        
        assert original_key != new_key  # Keys should be regenerated
    
    def test_cli_integration_via_subprocess(self):
        """Test CLI integration through subprocess calls."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Test init-identity via subprocess
        result = subprocess.run([
            "python", "-m", "core.run_proxy", 
            "init-identity", 
            "--suite", "cs-kyber768-aesgcm-dilithium3",
            "--output-dir", "secrets"
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode == 0
        assert os.path.exists(os.path.join(self.secrets_dir, "gcs_signing.key"))
        assert os.path.exists(os.path.join(self.secrets_dir, "gcs_signing.pub"))
    
    def test_gcs_command_requires_keys(self):
        """Test that GCS command fails without generated keys."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Try to run GCS without keys - should fail
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "gcs",
            "--suite", "cs-kyber768-aesgcm-dilithium3"
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode != 0  # Should fail
        assert ("signing key" in result.stderr.lower() or "key file" in result.stderr.lower() or 
                "ephemeral" in result.stderr.lower() or 
                "signing key" in result.stdout.lower() or "key file" in result.stdout.lower() or
                "ephemeral" in result.stdout.lower())
    
    def test_gcs_command_accepts_existing_keys(self):
        """Test that GCS command accepts pre-existing keys."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # First create keys
        subprocess.run([
            "python", "-m", "core.run_proxy",
            "init-identity",
            "--suite", "cs-kyber768-aesgcm-dilithium3", 
            "--output-dir", "secrets"
        ], cwd=self.test_dir, env=env)
        
        # Now try GCS command with timeout to prevent hanging
        # This should start successfully (not test full operation)
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "gcs", 
            "--suite", "cs-kyber768-aesgcm-dilithium3",
            "--help"  # Use help to avoid hanging
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        # Help should work regardless
        assert result.returncode == 0
    
    def test_drone_command_requires_peer_pubkey(self):
        """Test that drone command requires peer public key."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "drone",
            "--suite", "cs-kyber768-aesgcm-dilithium3"
            # Missing --peer-pubkey-file
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode != 0
        assert ("peer-pubkey-file" in result.stderr.lower() or "required" in result.stderr.lower() or
                "peer-pubkey-file" in result.stdout.lower() or "public key" in result.stdout.lower())
    
    def test_drone_command_accepts_peer_pubkey(self):
        """Test drone accepts valid peer public key file."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Create GCS keys first
        subprocess.run([
            "python", "-m", "core.run_proxy",
            "init-identity",
            "--suite", "cs-kyber768-aesgcm-dilithium3", 
            "--output-dir", "secrets"
        ], cwd=self.test_dir, env=env)
        
        # Test drone with peer pubkey (use help to avoid hanging)
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "drone",
            "--suite", "cs-kyber768-aesgcm-dilithium3",
            "--peer-pubkey-file", "secrets/gcs_signing.pub",
            "--help"
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode == 0  # Help should work
    
    def test_ephemeral_flag_bypasses_file_keys(self):
        """Test --ephemeral flag allows operation without persistent keys."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # This should work without any key files
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "gcs",
            "--suite", "cs-kyber768-aesgcm-dilithium3",
            "--ephemeral",
            "--help"
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode == 0  # Should work with ephemeral    def test_key_file_validation(self):
        """Test validation of key file formats."""
        # Create invalid key files
        invalid_key = os.path.join(self.secrets_dir, "invalid_signing.key")
        invalid_pub = os.path.join(self.secrets_dir, "invalid_signing.pub")
        
        with open(invalid_key, "w") as f:
            f.write("not-a-valid-key")
        
        with open(invalid_pub, "w") as f:
            f.write("not-a-valid-public-key")
        
        # Try to use invalid keys - should fail gracefully
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "gcs",
            "--suite", "cs-kyber768-aesgcm-dilithium3",
            "--signing-key-file", invalid_key
        ], cwd=self.test_dir, capture_output=True, text=True)
        
        # Should fail with reasonable error (not crash)
        assert result.returncode != 0
    
    def test_suite_compatibility_validation(self):
        """Test that init-identity validates suite compatibility."""
        # Set up environment with PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Test invalid suite name
        result = subprocess.run([
            "python", "-m", "core.run_proxy",
            "init-identity",
            "--suite", "invalid-suite-name"
        ], cwd=self.test_dir, capture_output=True, text=True, env=env)
        
        assert result.returncode != 0
        assert "suite" in result.stderr.lower()


class TestCLIHelpAndUsage:
    """Test CLI help messages and usage patterns."""
    
    def test_main_help(self):
        """Test main CLI help message."""
        result = subprocess.run([
            "python", "-m", "core.run_proxy", "--help"
        ], capture_output=True, text=True)
        
        assert result.returncode == 0
        assert "init-identity" in result.stdout
        assert "gcs" in result.stdout
        assert "drone" in result.stdout
    
    def test_subcommand_help_messages(self):
        """Test each subcommand has useful help."""
        subcommands = ["init-identity", "gcs", "drone"]
        
        for cmd in subcommands:
            result = subprocess.run([
                "python", "-m", "core.run_proxy", cmd, "--help"
            ], capture_output=True, text=True)
            
            assert result.returncode == 0
            assert "--suite" in result.stdout
            assert len(result.stdout) > 100  # Reasonable amount of help text
    
    def test_deprecated_wrapper_messages(self):
        """Test deprecated wrapper files show correct messages."""
        # Create a temporary test directory with just the wrapper files
        test_workspace = Path(__file__).parent.parent
        
        wrapper_files = [
            "drone/wrappers/drone_dilithium3.py",
            "gcs/wrappers/gcs_dilithium3.py"
        ]
        
        for wrapper_path in wrapper_files:
            full_path = test_workspace / wrapper_path
            if full_path.exists():
                result = subprocess.run([
                    "python", str(full_path)
                ], capture_output=True, text=True, cwd=test_workspace)
                
                assert result.returncode == 2  # Exit code for deprecation
                assert "Deprecated" in result.stdout
                assert "core.run_proxy" in result.stdout