"""
Intelligent Model Fallback Resolver

Provides dynamic fallback models for both remote APIs (OpenAI, Anthropic, Gemini) 
and local clusters (Ollama).
"""

import json
import urllib.request
import urllib.error

from utils.logger import get_logger

logger = get_logger("llm_client_fallback")

# Hardcoded cloud fallback maps (Prefix match -> Fallback model)
CLOUD_FALLBACKS = {
    "openai": [
        ("gpt-4.5", "gpt-4o"),
        ("gpt-4-", "gpt-4o"),
        ("gpt-3.5", "gpt-4o-mini"),
        ("gpt-4o", "gpt-4o-mini")
    ],
    "anthropic": [
        ("claude-3-5", "claude-3-7-sonnet"),
        ("claude-3-opus", "claude-3-7-sonnet"),
        ("claude-3-7-sonnet", "claude-3-5-sonnet"),
        ("claude-3-5-sonnet", "claude-3-5-haiku")
    ],
    "gemini": [
        ("gemini-1.5", "gemini-2.5-flash"),
        ("gemini-1.0", "gemini-2.5-flash"),
        ("gemini-2.5-pro", "gemini-2.5-flash")
    ]
}

def resolve_cloud_fallback(provider: str, failed_model: str) -> str | None:
    """Resolve a remote model fallback based on predefined mappings."""
    p = (provider or "").strip().lower()
    m = (failed_model or "").strip().lower()
    for prefix, fallback in CLOUD_FALLBACKS.get(p, []):
        if m.startswith(prefix) and m != fallback:
            return fallback
    return None

def _fetch_ollama_tags(base_url: str) -> list[str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return [m.get("name") for m in data.get("models", []) if m.get("name")]
    except Exception as e:
        logger.warning(f"Failed to fetch Ollama tags from {url}: {e}")
        return []

def resolve_ollama_fallback(failed_model: str, base_url: str) -> str | None:
    """
    Dynamically fetch available models from local Ollama and return the best 
    fallback based on an intelligent Tier list.
    """
    tags = _fetch_ollama_tags(base_url)
    if not tags:
        return None

    # Omit the failed model exactly
    available = [t for t in tags if t.lower() != failed_model.lower()]
    if not available:
        return None

    # Tiered ranking system based on Flume's optimal agent models
    tier_lists = [
        # Tier 1 (Coding Agents)
        ["deepseek-r1", "qwen2.5-coder", "deepseek-coder", "deepseek-coder-v2"],
        # Tier 2 (Frontier Multipurpose)
        ["llama3.3", "llama-3.3", "llama3.2", "llama3.1", "llama3"],
        # Tier 3 (Mid-weight Local)
        ["qwen2.5", "gemma2", "mistral", "mixtral", "phi4", "phi3"]
    ]

    for tier in tier_lists:
        for keyword in tier:
            for tag in available:
                # Return the very first match based on the keyword tier search
                if keyword in tag.lower():
                    return tag

    # Tier 4 (Fallback - grab the first available model that survived the filter)
    return available[0]

def resolve_fallback_model(provider: str, failed_model: str, base_url: str) -> str | None:
    """Wrapper that resolves a fallback model for any provider."""
    prov = (provider or "").strip().lower()
    if prov == "ollama":
        return resolve_ollama_fallback(failed_model, base_url)
    return resolve_cloud_fallback(prov, failed_model)
