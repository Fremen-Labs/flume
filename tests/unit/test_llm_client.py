"""
Unit tests for utils/llm_client_legacy.py — LLM Provider Resolution.

Tests the Gemini model normalization and provider identification
without requiring any running LLM endpoints.
"""
import sys
from pathlib import Path

# Add src/ to path so 'utils' package resolves as src/utils/
_SRC_ROOT = str(Path(__file__).resolve().parent.parent.parent / 'src')
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from utils import llm_client_legacy as llm_client

def test_normalize_gemini_model():
    """Verify Gemini's model-mapping architecture is strict."""
    assert llm_client._normalize_gemini_model("gemini-1.5-flash") == "gemini-2.5-flash"
    assert llm_client._normalize_gemini_model("gemini-1.5-pro") == "gemini-2.5-pro"
    assert llm_client._normalize_gemini_model("gemini-2.0-flash") == "gemini-2.5-flash"
    assert llm_client._normalize_gemini_model("unknown-model") == "unknown-model"

def test_provider_identification():
    """Verify robust LLM provider isolation based on environment."""
    assert llm_client._provider() in ["ollama", "openai", "anthropic", "gemini", "openai_compatible", "xai", "grok"]
