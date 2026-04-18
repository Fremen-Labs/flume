// ─── Real API types ───────────────────────────────────────────────────────────

export interface ApiProject {
  id: string;
  name: string;
  repoUrl: string;
  path: string;
  created_at: string;
  gitflow: { autoPrOnApprove: boolean; defaultBranch: string | null };
}

export interface ApiTask {
  _id: string;
  id: string;
  title: string;
  objective?: string;
  repo?: string;
  item_type?: 'epic' | 'feature' | 'story' | 'task';
  status: string;
  priority: string;
  owner?: string;
  depends_on?: string[];
  acceptance_criteria?: string[];
  branch?: string;
  target_branch?: string;
  pr_url?: string;
  pr_number?: string;
  pr_status?: string;
  pr_error?: string;
  commit_sha?: string;
  commit_message?: string;
  created_at?: string;
  updated_at?: string;
  last_update?: string;
  queue_state?: string;
  execution_host?: string;
  active_worker?: string;
  worktree?: string | null;
  execution_thoughts_count?: number;
}

export interface ApiWorker {
  name: string;
  role: string;
  model: string;
  execution_host: string;
  /** LLM provider for this worker (from agent_models.json + env). */
  llm_provider?: string;
  preferred_llm_credential_id?: string;
  /** Human-readable saved key / profile (from worker-manager state). */
  llm_credential_label?: string;
  status: string;
  current_task_id: string | null;
  current_task_title?: string;
  preferred_model?: string;
  preferred_llm_provider?: string;
  heartbeat_at?: string;
  input_tokens?: number;
  output_tokens?: number;
  /** ISO timestamp when the current task was claimed/started. */
  task_started_at?: string;
}

export interface ApiRepo {
  id: string;
  path: string;
  exists: boolean;
  is_git: boolean;
  current_branch?: string;
  last_commit?: { hash: string; author: string; date: string; subject: string };
}

export interface ApiReview {
  _id: string;
  review_id: string;
  task_id: string;
  verdict: string;
  summary: string;
  model_used?: string;
  created_at: string;
}

export interface ApiFailure {
  _id: string;
  id: string;
  task_id: string;
  project?: string;
  error_class: string;
  summary: string;
  created_at: string;
}

export interface Snapshot {
  workers: ApiWorker[];
  tasks: ApiTask[];
  reviews: ApiReview[];
  failures: ApiFailure[];
  provenance: unknown[];
  repos: ApiRepo[];
  projects: ApiProject[];
  elastro_savings?: number;
  token_metrics?: {
    savings: number;
    baseline_tokens: number;
    baseline_full_context_tokens: number;
    actual_tokens_sent: number;
    total_input_tokens: number;
    total_output_tokens: number;
  };
}

// ─── LLM Settings API ──────────────────────────────────────────────────────────

export interface LlmProviderModel {
  id: string;
  name: string;
}

export interface LlmProviderCatalogEntry {
  id: string;
  name: string;
  baseUrlDefault: string;
  authMode: string;
  models: LlmProviderModel[];
}

export interface LlmSettings {
  provider: string;
  model: string;
  baseUrl: string;
  authMode: 'api_key' | 'oauth';
  routeType: 'local' | 'network';
  host: string;
  port: number | null;
  basePath: string | null;
  apiKey?: string;
  /** Last 4 characters hint when apiKey is masked (***). */
  keySuffix?: string;
  /** Active saved credential id (llm_credentials.json), if any. */
  credentialId?: string;
  credentialLabel?: string;
  oauthStateFile: string;
  oauthTokenUrl: string;
}

export interface LlmCredentialSummary {
  id: string;
  label: string;
  provider: string;
  keySuffix: string;
  hasKey: boolean;
  baseUrl?: string;
}

/** From dashboard get_oauth_status — token shape / consent diagnostics */
export type LlmOAuthScopeStatus =
  | 'ok'
  | 'missing_responses_write'
  | 'jwt_no_scp'
  | 'opaque_or_unknown'
  | 'no_token';

export interface LlmOAuthStatus {
  configured: boolean;
  message?: string;
  hasAccessToken?: boolean;
  clientId?: string;
  expiresInSeconds?: number;
  /** JWT scp claim (decoded for display only) */
  accessTokenScopes?: string[];
  accessTokenAudience?: string;
  /** True if token has three dot-separated segments (may still be opaque to us). */
  accessTokenJwtLike?: boolean;
  /** True if we successfully base64-decoded the JWT payload. */
  accessTokenJwtParsed?: boolean;
  hasApiResponsesWrite?: boolean;
  /** JWT includes model.request (Codex browser OAuth typically does not). */
  hasModelRequestScope?: boolean;
  oauthScopesRequested?: string;
  /** Consent / token diagnostics; refresh alone will not upgrade this from missing → ok. */
  oauthScopeStatus?: LlmOAuthScopeStatus;
}

export interface LlmSettingsResponse {
  catalog: LlmProviderCatalogEntry[];
  settings: LlmSettings;
  /** Saved labeled API keys (worker-manager/llm_credentials.json). */
  credentials: LlmCredentialSummary[];
  activeCredentialId: string;
  /** Default saved key (same as activeCredentialId when omitted by older servers). */
  defaultCredentialId?: string;
  oauthStatus: LlmOAuthStatus;
  restartRequired: boolean;
  openbaoInstalled?: boolean;
}

export type LlmSettingsCatalogItem = LlmProviderCatalogEntry;

/** Payload for POST /api/settings/llm. */
export interface LlmSettingsPayload {
  provider: string;
  model: string;
  authMode?: 'api_key' | 'oauth';
  routeType?: 'local' | 'network';
  host?: string;
  port?: number;
  basePath?: string;
  baseUrl?: string;
  apiKey?: string;
  oauthStateFile?: string;
  oauthTokenUrl?: string;
  /** Label for the saved credential row when apiKey is sent. */
  credentialLabel?: string;
  /** When editing an existing saved credential’s key/label. */
  credentialId?: string;
}

/** POST /api/settings/llm/credentials */
export interface LlmCredentialActionPayload {
  action: 'upsert' | 'delete' | 'activate' | 'default' | 'patch';
  id?: string;
  label?: string;
  provider?: string;
  apiKey?: string;
  baseUrl?: string;
}

/** GET /api/codex-app-server/status */
export interface CodexAppServerStatusResponse {
  listenUrl: string;
  defaultListenUrl: string;
  codexBinary: string;
  codexResolvedPath: string | null;
  codexOnPath: boolean;
  npxOnPath?: boolean;
  npxResolvedPath?: string | null;
  /** True when ./flume codex-app-server will use npx @openai/codex (no global codex). */
  flumeWillUseNpxFallback?: boolean;
  tcpReachable: boolean | null;
  parseError: string | null;
  codexAuthFilePresent: boolean;
  docsUrl: string;
  envFlumeListen: string;
  envCodexBin: string;
}

/** GET /api/codex-app-server/proxy-config */
export interface CodexAppServerProxyConfigResponse {
  proxyWanted: boolean;
  proxyEnabled: boolean;
  proxyRunning: boolean;
  proxyPort: number;
  proxyBind: string;
  clientWsUrl: string;
  upstreamListenUrl: string;
  workspaceRoot: string;
  websocketsInstalled: boolean;
  websocketsImportError?: string | null;
  installHint?: string | null;
  disableReason?: string | null;
  serveError?: string | null;
}

// ─── Repo Settings API ─────────────────────────────────────────────────────

export interface GithubTokenPublic {
  id: string;
  label: string;
  tokenSuffix: string;
  hasToken: boolean;
}

export interface GithubTokenActionPayload {
  action: 'upsert' | 'delete' | 'setActive';
  id?: string;
  label?: string;
  token?: string;
}

export interface AdoCredentialPublic {
  id: string;
  label: string;
  /** Org URL stored with this PAT (e.g. https://dev.azure.com/myorg). */
  orgUrl: string;
  tokenSuffix: string;
  hasToken: boolean;
}

export interface AdoTokenActionPayload {
  action: 'upsert' | 'delete' | 'setActive';
  id?: string;
  label?: string;
  token?: string;
  orgUrl?: string;
}

export interface RepoSettings {
  /** Masked; mirrors whether the active GitHub PAT is set. */
  ghToken: string;
  githubTokens: GithubTokenPublic[];
  activeGithubTokenId: string;
  /** Masked; mirrors whether the active ADO PAT is set. */
  adoToken: string;
  /** Organization URL for the active ADO credential. */
  adoOrgUrl: string;
  adoCredentials: AdoCredentialPublic[];
  activeAdoCredentialId: string;
}

export interface RepoSettingsResponse {
  settings: RepoSettings;
  restartRequired: boolean;
}

export interface RepoSettingsPayload {
  /** @deprecated Prefer githubTokenAction + store; still accepted for legacy saves. */
  ghToken?: string;
  githubTokenAction?: GithubTokenActionPayload;
  /** @deprecated Prefer adoTokenAction + store; still accepted for legacy saves. */
  adoToken?: string;
  adoOrgUrl?: string;
  adoTokenAction?: AdoTokenActionPayload;
}

// ─── Per-role agent models (worker manager) ─────────────────────────────────

export interface AgentModelsProviderGroup {
  providerId: string;
  label: string;
  configured: boolean;
  isPrimary: boolean;
  models: LlmProviderModel[];
  allowCustomModelId?: boolean;
  hint?: string;
}

export interface AgentModelsRoleEffective {
  credentialId?: string;
  provider: string;
  model: string;
  executionHost: string;
}

export interface AgentModelsCredentialGroup {
  credentialId: string;
  label: string;
  shortLabel?: string;
  providerId: string;
  configured: boolean;
  /** Last 4 chars of key when present (for disambiguation in UI). */
  keySuffix?: string;
  models: LlmProviderModel[];
  allowCustomModelId?: boolean;
  hint?: string;
}

export interface AgentModelsResponse {
  defaultLlmModel: string;
  defaultExecutionHost: string;
  settingsProvider: string;
  roles: Record<string, AgentModelsRoleEffective | string>;
  effective: Record<string, AgentModelsRoleEffective>;
  availableProviders: AgentModelsProviderGroup[];
  /** Prefer this for per-agent provider+key selection. */
  availableCredentials?: AgentModelsCredentialGroup[];
  roleIds: string[];
}

export interface AgentModelsSavePayload {
  roles: Record<
    string,
    | { credentialId: string; provider?: string; model: string; executionHost?: string }
    | { provider: string; model: string; executionHost?: string }
    | string
    | null
  >;
}

// ─── Legacy mock types (kept for compatibility) ───────────────────────────────

export type ProjectStatus = 'active' | 'planning' | 'paused' | 'completed' | 'archived';
export type Priority = 'critical' | 'high' | 'medium' | 'low';
export type WorkItemStatus = 'backlog' | 'intake' | 'breakdown' | 'architecture' | 'story_writing' | 'task_generation' | 'in_progress' | 'code_review' | 'qa' | 'done' | 'blocked';
export type AgentStatus = 'idle' | 'active' | 'waiting' | 'blocked' | 'failed' | 'offline';
export type AgentRole = 'project_manager' | 'product_owner' | 'architect' | 'backend_engineer' | 'frontend_engineer' | 'devops_engineer' | 'qa_engineer' | 'code_reviewer' | 'scheduler' | 'documentation';
export type HealthStatus = 'healthy' | 'at_risk' | 'critical';

export interface Project {
  id: string;
  name: string;
  description: string;
  type: string;
  status: ProjectStatus;
  priority: Priority;
  progress: number;
  health: HealthStatus;
  activeAgents: number;
  queuedItems: number;
  activeItems: number;
  completedItems: number;
  blockedItems: number;
  totalItems: number;
  createdAt: string;
  updatedAt: string;
  repoUrl?: string;
}

export interface Epic {
  id: string;
  projectId: string;
  title: string;
  description: string;
  status: WorkItemStatus;
  progress: number;
  features: Feature[];
}

export interface Feature {
  id: string;
  epicId: string;
  title: string;
  status: WorkItemStatus;
  stories: Story[];
}

export interface Story {
  id: string;
  featureId: string;
  title: string;
  acceptanceCriteria: string[];
  status: WorkItemStatus;
  assignedAgentId?: string;
  tasks: Task[];
}

export interface Task {
  id: string;
  storyId: string;
  title: string;
  status: WorkItemStatus;
  assignedAgentId?: string;
  estimate?: number;
  startedAt?: string;
  completedAt?: string;
}

export interface Agent {
  id: string;
  name: string;
  role: AgentRole;
  specialty: string;
  status: AgentStatus;
  currentTaskId?: string;
  currentTaskTitle?: string;
  currentProjectId?: string;
  currentProjectName?: string;
  utilization: number;
  lastHeartbeat: string;
  queueDepth: number;
  handoffCount: number;
  successRate: number;
  tasksCompleted: number;
}

export interface ActivityEvent {
  id: string;
  type: 'created' | 'claimed' | 'completed' | 'handed_off' | 'blocked' | 'reviewed' | 'deployed' | 'reprioritized' | 'failed';
  actorAgentId: string;
  actorAgentName: string;
  actorRole: AgentRole;
  relatedEntityType: 'project' | 'epic' | 'feature' | 'story' | 'task';
  relatedEntityId: string;
  relatedEntityTitle: string;
  projectName: string;
  timestamp: string;
  message: string;
}

export interface QueueStage {
  id: WorkItemStatus;
  label: string;
  items: QueueItem[];
}

export interface QueueItem {
  id: string;
  title: string;
  type: 'epic' | 'feature' | 'story' | 'task';
  projectName: string;
  assignedAgent?: string;
  priority: Priority;
  status: WorkItemStatus;
  createdAt: string;
}

export interface HandoffEvent {
  id: string;
  fromAgentId: string;
  fromAgentName: string;
  fromRole: AgentRole;
  toAgentId: string;
  toAgentName: string;
  toRole: AgentRole;
  workItemTitle: string;
  workItemType: 'epic' | 'feature' | 'story' | 'task';
  projectName: string;
  timestamp: string;
  status: 'completed' | 'pending' | 'rejected';
}

export interface SystemMetrics {
  healthScore: number;
  throughputScore: number;
  blockedRatio: number;
  velocityTrend: number;
  reviewTurnaround: number;
  testingPassRate: number;
  agentResponseTime: number;
  completionVelocity: number;
}

export interface ThroughputCell {
  stage: string;
  period: string;
  value: number;
}

export interface BottleneckItem {
  id: string;
  severity: 'critical' | 'high' | 'medium';
  category: string;
  affectedProject: string;
  affectedStage: string;
  description: string;
  suggestedAction: string;
}
