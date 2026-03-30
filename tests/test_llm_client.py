from utils import llm_client

def test_normalize_gemini_model():
    """Verify Gemini's model-mapping architecture is strict."""
    assert llm_client._normalize_gemini_model("gemini-1.5-flash") == "gemini-2.5-flash"
    assert llm_client._normalize_gemini_model("gemini-1.5-pro") == "gemini-2.5-pro"
    assert llm_client._normalize_gemini_model("gemini-2.0-flash") == "gemini-2.5-flash"
    assert llm_client._normalize_gemini_model("unknown-model") == "unknown-model"

def test_provider_identification():
    """Verify robust LLM provider isolation based on environment."""
    assert llm_client._PROVIDER in ["ollama", "openai", "anthropic", "gemini", "openai_compatible"]
