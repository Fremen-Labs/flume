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
}

export interface ApiWorker {
  name: string;
  role: string;
  model: string;
  execution_host: string;
  /** LLM provider for this worker (from agent_models.json + env). */
  llm_provider?: string;
  status: string;
  current_task_id: string | null;
  current_task_title?: string;
  preferred_model?: string;
  preferred_llm_provider?: string;
  heartbeat_at?: string;
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
  oauthStateFile: string;
  oauthTokenUrl: string;
}

export interface LlmOAuthStatus {
  configured: boolean;
  message?: string;
  hasAccessToken?: boolean;
  clientId?: string;
  expiresInSeconds?: number;
  /** JWT scp claim (decoded for display only) */
  accessTokenScopes?: string[];
  accessTokenAudience?: string;
  hasApiResponsesWrite?: boolean;
  oauthScopesRequested?: string;
}

export interface LlmSettingsResponse {
  catalog: LlmProviderCatalogEntry[];
  settings: LlmSettings;
  oauthStatus: LlmOAuthStatus;
  restartRequired: boolean;
  openbaoInstalled?: boolean;
}

export type LlmSettingsCatalogItem = LlmProviderCatalogEntry;

export interface LlmSettingsPayload {
  provider: string;
  model: string;
  authMode: 'api_key' | 'oauth';
  routeType: 'local' | 'network';
  host?: string;
  port?: number;
  basePath?: string;
  baseUrl?: string;
  apiKey?: string;
  oauthStateFile?: string;
  oauthTokenUrl?: string;
}

/** Alias for provider catalog items (used by SettingsPage). */
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
}

// ─── Repo Settings API ─────────────────────────────────────────────────────

export interface RepoSettings {
  ghToken: string;
  adoToken: string;
  adoOrgUrl: string;
}

export interface RepoSettingsResponse {
  settings: RepoSettings;
  restartRequired: boolean;
}

export interface RepoSettingsPayload {
  ghToken?: string;
  adoToken?: string;
  adoOrgUrl?: string;
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
  provider: string;
  model: string;
  executionHost: string;
}

export interface AgentModelsResponse {
  defaultLlmModel: string;
  defaultExecutionHost: string;
  settingsProvider: string;
  roles: Record<string, AgentModelsRoleEffective | string>;
  effective: Record<string, AgentModelsRoleEffective>;
  availableProviders: AgentModelsProviderGroup[];
  roleIds: string[];
}

export interface AgentModelsSavePayload {
  roles: Record<
    string,
    { provider: string; model: string; executionHost?: string } | string | null
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
