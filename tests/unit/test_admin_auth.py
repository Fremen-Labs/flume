"""
Unit tests for Admin Authorization — Security Layer.

Tests the AdminAuthorizer pattern used to protect the kill switch
and other admin-only endpoints (stop-all, resume-all, sync-ast).

Follows OWASP Zero-Trust testing: verify both positive and negative
authentication paths, including timing-safe comparison behavior.
"""
import pytest

# Path setup and module mocking handled by conftest.py
from server import AdminAuthorizer, AuthConfigurationError, InvalidCredentialsError


# ═══════════════════════════════════════════════════════════════════════════════
# AdminAuthorizer — Token Validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdminAuthorizer:
    """Validates admin token authorization logic."""

    def test_valid_token_passes(self):
        """Correct Bearer token should not raise any exception."""
        auth = AdminAuthorizer("my-secret-token")
        auth.authorize("Bearer my-secret-token")  # Should not raise

    def test_invalid_token_raises(self):
        """Wrong token should raise InvalidCredentialsError."""
        auth = AdminAuthorizer("correct-token")
        with pytest.raises(InvalidCredentialsError):
            auth.authorize("Bearer wrong-token")

    def test_missing_auth_header_raises(self):
        """None auth header should raise InvalidCredentialsError."""
        auth = AdminAuthorizer("my-token")
        with pytest.raises(InvalidCredentialsError):
            auth.authorize(None)

    def test_empty_auth_header_raises(self):
        """Empty string auth header should raise InvalidCredentialsError."""
        auth = AdminAuthorizer("my-token")
        with pytest.raises(InvalidCredentialsError):
            auth.authorize("")

    def test_no_bearer_prefix_raises(self):
        """Token without 'Bearer ' prefix should fail."""
        auth = AdminAuthorizer("my-token")
        with pytest.raises(InvalidCredentialsError):
            auth.authorize("my-token")  # Missing "Bearer " prefix

    def test_empty_required_token_raises_config_error(self):
        """If the server has no admin token configured, raise AuthConfigurationError."""
        auth = AdminAuthorizer("")
        with pytest.raises(AuthConfigurationError):
            auth.authorize("Bearer anything")

    def test_none_required_token_raises_config_error(self):
        """None required_token means admin endpoint is disabled."""
        auth = AdminAuthorizer(None)
        with pytest.raises(AuthConfigurationError):
            auth.authorize("Bearer anything")

    def test_timing_safe_comparison(self):
        """Verify the comparison uses secrets.compare_digest (timing-safe).
        We can't directly test timing safety, but we can verify the behavior
        is consistent — wrong tokens of same length still fail."""
        auth = AdminAuthorizer("abcdefgh12345678")
        with pytest.raises(InvalidCredentialsError):
            auth.authorize("Bearer abcdefgh12345679")  # One char different, same length
