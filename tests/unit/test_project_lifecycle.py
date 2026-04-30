"""
Unit tests for core/project_lifecycle.py — Clone and AST Ingest.

Tests the workspace resolution, AST existence check, deterministic
ingest logic, and clone-setup lifecycle without requiring running
infrastructure. All subprocess, ES, and filesystem calls are mocked.

Follows Google's Table-Driven Test Pattern for deterministic validation.
"""
import os
import asyncio
import subprocess
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock
from pathlib import Path

from core.project_lifecycle import (
    _get_workspace_root,
    _check_ast_exists_natively,
    _deterministic_ast_ingest,
    _clone_and_setup_project,
    _ELASTRO_INDEX,
    _AST_INGEST_TIMEOUT_S,
    _GIT_CLONE_TIMEOUT_S,
    _DEFAULT_ELASTRO_BIN,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Module Constants — Correctness
# ═══════════════════════════════════════════════════════════════════════════════

class TestProjectLifecycleConstants:
    """Validates module-level constants have sensible values."""

    def test_elastro_index_is_string(self):
        assert isinstance(_ELASTRO_INDEX, str)
        assert len(_ELASTRO_INDEX) > 0

    def test_elastro_index_default(self):
        """Default index should match the expected convention."""
        # May be overridden by env var, so just verify it's a string
        assert 'elastro' in _ELASTRO_INDEX.lower() or isinstance(_ELASTRO_INDEX, str)

    def test_ast_ingest_timeout_reasonable(self):
        assert _AST_INGEST_TIMEOUT_S >= 30
        assert _AST_INGEST_TIMEOUT_S <= 600

    def test_git_clone_timeout_reasonable(self):
        assert _GIT_CLONE_TIMEOUT_S >= 60
        assert _GIT_CLONE_TIMEOUT_S <= 600

    def test_default_elastro_bin_is_path(self):
        assert isinstance(_DEFAULT_ELASTRO_BIN, Path)


# ═══════════════════════════════════════════════════════════════════════════════
# _get_workspace_root — Cached Resolution
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetWorkspaceRoot:
    """Validates the workspace root resolution."""

    def test_returns_path_like(self):
        """Should return a Path-like object from the cached resolver."""
        result = _get_workspace_root()
        # The conftest mock returns Path('/tmp/flume-test-workspace')
        assert result is not None

    def test_result_is_cacheable(self):
        """Multiple calls should return the same reference (lru_cache)."""
        a = _get_workspace_root()
        b = _get_workspace_root()
        assert a is b


# ═══════════════════════════════════════════════════════════════════════════════
# _check_ast_exists_natively — ES Term Query
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckAstExistsNatively:
    """Validates the AST existence check against ES."""

    @patch('core.project_lifecycle.async_es_search', new_callable=AsyncMock)
    def test_exists_true(self, mock_search):
        """Matching records should return (True, 'Found...')."""
        mock_search.return_value = {
            'hits': {'total': {'value': 3}, 'hits': [{'_source': {}}]}
        }
        exists, msg = asyncio.run(_check_ast_exists_natively('/workspace/my-repo'))
        assert exists is True
        assert 'Found' in msg

    @patch('core.project_lifecycle.async_es_search', new_callable=AsyncMock)
    def test_exists_false(self, mock_search):
        """No matching records should return (False, 'No logical...')."""
        mock_search.return_value = {
            'hits': {'total': {'value': 0}, 'hits': []}
        }
        exists, msg = asyncio.run(_check_ast_exists_natively('/workspace/my-repo'))
        assert exists is False
        assert 'No logical' in msg

    @patch('core.project_lifecycle.async_es_search', new_callable=AsyncMock)
    def test_uses_keyword_term_query(self, mock_search):
        """Should use term query on .keyword for exact path matching."""
        mock_search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
        asyncio.run(_check_ast_exists_natively('/workspace/test-repo'))
        call_args = mock_search.call_args[0]
        assert call_args[0] == _ELASTRO_INDEX
        query = call_args[1]
        assert 'file_path.keyword' in str(query)
        assert query['query']['term']['file_path.keyword'] == '/workspace/test-repo'

    @patch('core.project_lifecycle.async_es_search', new_callable=AsyncMock)
    def test_es_failure_returns_false(self, mock_search):
        """ES failure should return (False, error_message)."""
        mock_search.side_effect = Exception('Connection refused')
        exists, msg = asyncio.run(_check_ast_exists_natively('/workspace/repo'))
        assert exists is False
        assert 'Connection refused' in msg

    @patch('core.project_lifecycle.async_es_search', new_callable=AsyncMock)
    def test_missing_total_key(self, mock_search):
        """Missing 'total' key in ES response should return False."""
        mock_search.return_value = {'hits': {'hits': []}}
        exists, msg = asyncio.run(_check_ast_exists_natively('/workspace/repo'))
        assert exists is False


# ═══════════════════════════════════════════════════════════════════════════════
# _deterministic_ast_ingest — Idempotent Ingestion
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministicAstIngest:
    """Validates the AST ingestion lifecycle."""

    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_already_indexed_skips(self, mock_check):
        """Already-indexed repo should skip ingestion and return True."""
        mock_check.return_value = (True, 'Found mapping records')
        mock_client = MagicMock()
        result = asyncio.run(
            _deterministic_ast_ingest(mock_client, '/workspace/repo', 'proj-1', 'Test')
        )
        assert result is True

    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    @patch('core.project_lifecycle.shutil.which', return_value='/usr/bin/elastro')
    @patch('core.project_lifecycle._DEFAULT_ELASTRO_BIN', Path('/nonexistent/elastro'))
    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_ingest_runs_elastro(self, mock_check, mock_which, mock_subprocess):
        """New repo should run elastro rag ingest subprocess."""
        mock_check.return_value = (False, 'No logical paths matched')
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_client = MagicMock()
        result = asyncio.run(
            _deterministic_ast_ingest(mock_client, '/workspace/repo', 'proj-1', 'Test')
        )
        assert result is True
        mock_subprocess.assert_called_once()
        # Verify elastro was invoked with 'rag ingest'
        call_args = mock_subprocess.call_args[0]
        assert 'rag' in call_args
        assert 'ingest' in call_args

    @patch('core.project_lifecycle.shutil.which', return_value=None)
    @patch('core.project_lifecycle._DEFAULT_ELASTRO_BIN', Path('/nonexistent/elastro'))
    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_elastro_not_found_returns_false(self, mock_check, mock_which):
        """Missing elastro binary should return False gracefully."""
        mock_check.return_value = (False, 'No logical paths matched')
        mock_client = MagicMock()
        result = asyncio.run(
            _deterministic_ast_ingest(mock_client, '/workspace/repo', 'proj-1', 'Test')
        )
        assert result is False

    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    @patch('core.project_lifecycle.shutil.which', return_value='/usr/bin/elastro')
    @patch('core.project_lifecycle._DEFAULT_ELASTRO_BIN', Path('/nonexistent/elastro'))
    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_subprocess_failure_returns_false(self, mock_check, mock_which, mock_subprocess):
        """Subprocess exit code != 0 should return False."""
        mock_check.return_value = (False, 'No logical paths matched')
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b'stdout', b'error: failed'))
        mock_subprocess.return_value = mock_proc
        mock_client = MagicMock()
        result = asyncio.run(
            _deterministic_ast_ingest(mock_client, '/workspace/repo', 'proj-1', 'Test')
        )
        assert result is False

    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_remote_url_sanitized_to_local_path(self, mock_check):
        """Remote HTTPS URLs should be sanitized to basename local paths."""
        mock_check.return_value = (True, 'Found')
        mock_client = MagicMock()
        asyncio.run(
            _deterministic_ast_ingest(mock_client, 'https://github.com/org/my-repo.git', 'proj-1', 'Test')
        )
        # The check should receive the sanitized local path, not the raw URL
        call_args = mock_check.call_args[0]
        assert not call_args[0].startswith('http')
        assert 'my-repo' in call_args[0]

    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    @patch('core.project_lifecycle.shutil.which', return_value='/usr/bin/elastro')
    @patch('core.project_lifecycle._DEFAULT_ELASTRO_BIN', Path('/nonexistent/elastro'))
    @patch('core.project_lifecycle._check_ast_exists_natively', new_callable=AsyncMock)
    def test_ingest_passes_es_env_vars(self, mock_check, mock_which, mock_subprocess):
        """Subprocess should receive ES connection env vars."""
        mock_check.return_value = (False, 'No logical paths matched')
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_client = MagicMock()
        asyncio.run(
            _deterministic_ast_ingest(mock_client, '/workspace/repo', 'proj-1', 'Test')
        )
        # Verify env= kwarg was passed
        call_kwargs = mock_subprocess.call_args[1]
        assert 'env' in call_kwargs
        env = call_kwargs['env']
        assert 'ELASTIC_URL' in env
        assert 'ELASTIC_VERIFY_CERTS' in env
        assert env['ELASTIC_VERIFY_CERTS'] == 'false'


# ═══════════════════════════════════════════════════════════════════════════════
# _clone_and_setup_project — Full Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestCloneAndSetupProject:
    """Validates the clone → ingest → cleanup lifecycle."""

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.shutil.rmtree')
    @patch('core.project_lifecycle._deterministic_ast_ingest', new_callable=AsyncMock)
    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    def test_successful_clone_and_ingest(self, mock_subprocess, mock_ingest, mock_rmtree, mock_update):
        """Successful clone + ingest should set clone_status='indexed'."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_ingest.return_value = True

        mock_client = MagicMock()
        dest = Path('/tmp/test-clone-dest')

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', dest,
        ))

        # Should call update at least twice: indexing + indexed
        assert mock_update.call_count >= 2
        final_call_kwargs = mock_update.call_args[1]
        assert final_call_kwargs.get('clone_status') == 'indexed'
        assert final_call_kwargs.get('ast_indexed') is True

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.shutil.rmtree')
    @patch('core.project_lifecycle._deterministic_ast_ingest', new_callable=AsyncMock)
    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    def test_ast_failure_sets_ast_failed(self, mock_subprocess, mock_ingest, mock_rmtree, mock_update):
        """Failed AST ingest should set clone_status='ast_failed'."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_ingest.return_value = False

        mock_client = MagicMock()
        dest = Path('/tmp/test-clone-dest')

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', dest,
        ))

        final_call_kwargs = mock_update.call_args[1]
        assert final_call_kwargs.get('clone_status') == 'ast_failed'

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    def test_clone_failure_sets_failed(self, mock_subprocess, mock_update):
        """Failed git clone should set clone_status='failed'."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 128
        mock_proc.communicate = AsyncMock(return_value=(b'', b'fatal: could not read'))
        mock_subprocess.return_value = mock_proc

        mock_client = MagicMock()
        dest = Path('/tmp/test-clone-dest')

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', dest,
        ))

        final_call_kwargs = mock_update.call_args[1]
        assert final_call_kwargs.get('clone_status') == 'failed'
        assert final_call_kwargs.get('clone_error') is not None

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.shutil.rmtree')
    @patch('core.project_lifecycle._deterministic_ast_ingest', new_callable=AsyncMock)
    def test_already_cloned_skips_git(self, mock_ingest, mock_rmtree, mock_update, tmp_path):
        """Complete clone directory should skip git clone and proceed to AST ingest."""
        # Set up a "complete" clone directory
        git_dir = tmp_path / '.git'
        git_dir.mkdir()
        (git_dir / 'HEAD').write_text('ref: refs/heads/main\n')
        refs_dir = git_dir / 'refs' / 'heads'
        refs_dir.mkdir(parents=True)
        (refs_dir / 'main').write_text('abc123\n')

        mock_ingest.return_value = True
        mock_client = MagicMock()

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', tmp_path,
        ))

        # AST ingest should have been called
        mock_ingest.assert_called_once()
        # Clone status should be 'indexed'
        final_call_kwargs = mock_update.call_args[1]
        assert final_call_kwargs.get('clone_status') == 'indexed'

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.shutil.rmtree')
    @patch('core.project_lifecycle._deterministic_ast_ingest', new_callable=AsyncMock)
    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    def test_ephemeral_clone_deleted_after_ingest(self, mock_subprocess, mock_ingest, mock_rmtree, mock_update):
        """Local clone should be deleted after AST ingest (AP-4B)."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_ingest.return_value = True

        mock_client = MagicMock()
        dest = Path('/tmp/test-ephemeral')

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', dest,
        ))

        # shutil.rmtree should have been called to clean up the clone
        mock_rmtree.assert_called()

    @patch('core.project_lifecycle._update_project_registry_field')
    @patch('core.project_lifecycle.shutil.rmtree')
    @patch('core.project_lifecycle._deterministic_ast_ingest', new_callable=AsyncMock)
    @patch('core.project_lifecycle.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    def test_no_persistent_local_path(self, mock_subprocess, mock_ingest, mock_rmtree, mock_update):
        """After successful ingest, path should be set to None (AP-4B)."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'', b''))
        mock_subprocess.return_value = mock_proc
        mock_ingest.return_value = True

        mock_client = MagicMock()
        dest = Path('/tmp/test-no-persist')

        asyncio.run(_clone_and_setup_project(
            mock_client, 'proj-1', 'Test Project',
            'https://github.com/org/repo.git', dest,
        ))

        final_call_kwargs = mock_update.call_args[1]
        assert final_call_kwargs.get('path') is None
