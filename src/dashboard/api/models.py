from typing import Any, Optional, List
from pydantic import BaseModel, ConfigDict, Field


# ── Existing models ────────────────────────────────────────────────────────────

class ProjectCreateRequest(BaseModel):
    name: str = Field(..., description="Project name")
    repoUrl: Optional[str] = None
    localPath: Optional[str] = None

class TaskTransitionRequest(BaseModel):
    status: str
    instruction: Optional[str] = ""
    auto_recovery_prompt: Optional[bool] = True

class BulkRequeueRequest(BaseModel):
    task_ids: List[str]

class BulkUpdateRequest(BaseModel):
    ids: List[str]
    action: str
    repo: Optional[str] = ""


# ── Logging ────────────────────────────────────────────────────────────────────

class LogLevelRequest(BaseModel):
    level: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

class ClientLogRequest(BaseModel):
    level: str = Field(default="ERROR", description="Browser log severity")
    message: str = Field(default="Unknown client error")
    data: Optional[dict[str, Any]] = Field(default_factory=dict)


# ── LLM Settings ───────────────────────────────────────────────────────────────
# These pass through to validate_llm_settings / apply_credentials_action which
# accept an open dict[str, Any]. ConfigDict(extra='allow') preserves all keys
# while still giving FastAPI a typed schema and rejecting non-dict bodies.

class LLMSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: Optional[str] = None
    model: Optional[str] = None
    authMode: Optional[str] = None
    routeType: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    basePath: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    credentialLabel: Optional[str] = None
    credentialId: Optional[str] = None

class LLMCredentialsActionRequest(BaseModel):
    """Payload for POST /api/settings/llm/credentials (credentials store action)."""
    model_config = ConfigDict(extra="allow")

    action: Optional[str] = None
    credentialId: Optional[str] = None


# ── Repo Settings ──────────────────────────────────────────────────────────────

class RepoSettingsRequest(BaseModel):
    """Payload for PUT /api/settings/repos — open schema forwarded to update_repo_settings."""
    model_config = ConfigDict(extra="allow")

    githubTokenAction: Optional[dict[str, Any]] = None
    adoTokenAction: Optional[dict[str, Any]] = None
    ghToken: Optional[str] = None
    adoToken: Optional[str] = None
    adoOrgUrl: Optional[str] = None


# ── Agent Models ───────────────────────────────────────────────────────────────

class AgentModelsRequest(BaseModel):
    """Payload for PUT/POST /api/settings/agent-models."""
    model_config = ConfigDict(extra="allow")

    roles: Optional[dict[str, Any]] = Field(default_factory=dict)


# ── Intake ─────────────────────────────────────────────────────────────────────

class IntakeSessionRequest(BaseModel):
    repo: str = Field(..., description="Project/repo ID to plan against")
    prompt: str = Field(..., description="Initial planning prompt from the user")

class IntakeMessageRequest(BaseModel):
    text: str = Field(..., description="Refinement message from the user")
    plan: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional current draft plan to refine against"
    )

class IntakeCommitRequest(BaseModel):
    plan: Optional[dict[str, Any]] = Field(
        default=None,
        description="Final plan to commit; falls back to session draftPlan if omitted"
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repo ID override; falls back to session repo"
    )


# ── System Settings ────────────────────────────────────────────────────────────

class SystemSettingsRequest(BaseModel):
    es_url: str
    es_api_key: str
    es_verify_tls: Optional[bool] = None
    openbao_url: str
    vault_token: str
    prometheus_enabled: bool


# ── Task Claims ────────────────────────────────────────────────────────────────

class TaskClaimRequest(BaseModel):
    worker_id: str
