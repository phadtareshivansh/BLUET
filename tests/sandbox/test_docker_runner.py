"""Docker sandbox runner integration tests.

These tests require a running Docker daemon and are marked with @pytest.mark.docker
so they can be skipped in CI environments without Docker.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from bluet.sandbox.docker import DockerSandboxRunner, create_sandbox_runner
from bluet.sandbox.runtime import RuntimeLimits, BACKEND_GVISOR, BACKEND_HARDENED
from bluet.sandbox.execution import ExecutionResult, LocalRunner


@pytest.mark.docker
class TestDockerSandboxRunner:
    """Integration tests for DockerSandboxRunner (require Docker daemon)."""

    def test_python_runner_creation(self):
        """Test that Python runner can be created."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        assert runner is not None
        assert runner._backend == BACKEND_HARDENED

    def test_java_runner_creation(self):
        """Test that Java runner can be created."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/java:latest",
        )
        assert runner is not None

    def test_hardened_docker_flags(self):
        """Test that hardened Docker flags are correctly generated."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(memory="512m", network="none"),
            image_tag="bluet/python:latest",
        )
        host_config = runner._get_host_config()
        
        assert host_config["network_mode"] == "none"
        assert host_config["read_only"] is True
        assert "ALL" in host_config["cap_drop"]
        assert host_config["mem_limit"] == "512m"
        assert host_config["runtime"] == "runc"

    @patch("bluet.sandbox.docker.docker.DockerClient")
    def test_gvisor_flags(self, mock_docker_client):
        """Test that gVisor flags are correctly generated."""
        runner = DockerSandboxRunner(
            backend=BACKEND_GVISOR,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
            docker_client=mock_docker_client,
        )
        host_config = runner._get_host_config()
        
        assert host_config["runtime"] == "runsc"
        assert host_config["network_mode"] == "none"
        assert host_config["read_only"] is True

    @pytest.mark.asyncio
    async def test_python_execution(self):
        """Test running a simple Python script in the sandbox."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        
        files = {
            "test.py": "print('hello from sandbox')",
        }
        
        result = await runner.run(
            files,
            ["-c", "print('hello from sandbox')"],
            timeout=30.0,
        )
        
        assert result.returncode == 0
        assert "hello from sandbox" in result.stdout

    @pytest.mark.asyncio
    async def test_python_execution_with_files(self):
        """Test running Python with multiple files."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        
        files = {
            "main.py": "from helper import greet\nprint(greet('world'))",
            "helper.py": "def greet(name):\n    return f'Hello, {name}!'",
        }
        
        result = await runner.run(
            files,
            ["main.py"],
            timeout=30.0,
        )
        
        assert result.returncode == 0
        assert "Hello, world!" in result.stdout

    @pytest.mark.asyncio
    async def test_timeout_enforcement(self):
        """Test that timeout is enforced."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        
        files = {
            "infinite.py": "import time\nwhile True:\n    time.sleep(1)",
        }
        
        result = await runner.run(
            files,
            ["infinite.py"],
            timeout=2.0,
        )
        
        assert result.timed_out is True
        assert result.returncode != 0

    @pytest.mark.asyncio
    async def test_memory_limit_enforcement(self):
        """Test that memory limit is enforced."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(memory="50m"),
            image_tag="bluet/python:latest",
        )
        
        # Try to allocate more than 50MB
        files = {
            "oom.py": "data = 'x' * (100 * 1024 * 1024)\nprint(len(data))",
        }
        
        result = await runner.run(
            files,
            ["oom.py"],
            timeout=30.0,
        )
        
        # Should be killed due to OOM
        assert result.returncode != 0


@pytest.mark.unit
class TestDockerSandboxRunnerUnit:
    """Unit tests for DockerSandboxRunner (mocked Docker)."""

    def test_create_tar_archive(self):
        """Test tar archive creation."""
        runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        
        files = {
            "test.py": "print('hello')",
            "sub/module.py": "def foo(): pass",
        }
        
        tar_data = runner._create_tar_archive(files)
        assert len(tar_data) > 0
        
        # Verify it's a valid tar
        import tarfile
        import io
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r") as tar:
            names = tar.getnames()
            assert "test.py" in names
            assert "sub/module.py" in names

    def test_image_tag_construction(self):
        """Test that image tags are constructed correctly."""
        python_runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/python:latest",
        )
        assert "python" in python_runner._image_tag
        
        java_runner = DockerSandboxRunner(
            backend=BACKEND_HARDENED,
            host_backend="native",
            limits=RuntimeLimits(),
            image_tag="bluet/java:latest",
        )
        assert "java" in java_runner._image_tag


@pytest.mark.unit
class TestCreateSandboxRunner:
    """Tests for create_sandbox_runner factory."""

    def test_creates_local_runner_when_no_job_backend(self):
        """Test factory creates LocalRunner when job has no backend."""
        mock_job = MagicMock()
        mock_job.backend = None
        
        with patch("bluet.sandbox.docker.diagnose") as mock_diagnose:
            from bluet.sandbox.runtime import Diagnostics
            mock_diagnose.return_value = Diagnostics(
                docker_ok=True,
                docker_error=None,
                gvisor_available=False,
                backend=BACKEND_HARDENED,
                limits=RuntimeLimits(),
                host_backend="native",
            )
            
            with patch("bluet.sandbox.docker.DockerSandboxRunner", side_effect=Exception("Docker not available")):
                runner = create_sandbox_runner(mock_job, None)
                assert isinstance(runner, LocalRunner)

    def test_creates_docker_runner_when_backend_available(self):
        """Test factory creates DockerSandboxRunner when backend available."""
        mock_job = MagicMock()
        mock_job.backend = BACKEND_HARDENED
        
        with patch("bluet.sandbox.docker.DockerSandboxRunner") as mock_docker_runner:
            mock_runner = MagicMock()
            mock_docker_runner.return_value = mock_runner
            
            runner = create_sandbox_runner(mock_job, None)
            assert runner == mock_runner
            mock_docker_runner.assert_called_once()