import type { Project, Agent, ActivityEvent, QueueItem, Epic, AgentRole, AgentStatus, WorkItemStatus, Priority, HandoffEvent, SystemMetrics, ThroughputCell, BottleneckItem } from '@/types';

export const projects: Project[] = [
  {
    id: 'proj-1', name: 'Nexus Platform', description: 'Next-gen microservices platform with AI-driven scaling', type: 'Platform', status: 'active', priority: 'critical', progress: 67, health: 'healthy',
    activeAgents: 8, queuedItems: 14, activeItems: 12, completedItems: 45, blockedItems: 2, totalItems: 73, createdAt: '2026-02-01T10:00:00Z', updatedAt: '2026-03-16T08:30:00Z',
    repoUrl: 'https://github.com/expressjs/express',
  },
  {
    id: 'proj-2', name: 'Quantum Analytics', description: 'Real-time analytics dashboard with ML-powered insights', type: 'Analytics', status: 'active', priority: 'high', progress: 42, health: 'at_risk',
    activeAgents: 5, queuedItems: 22, activeItems: 8, completedItems: 28, blockedItems: 5, totalItems: 63, createdAt: '2026-02-10T14:00:00Z', updatedAt: '2026-03-16T07:45:00Z',
    repoUrl: 'https://github.com/recharts/recharts',
  },
  {
    id: 'proj-3', name: 'Sentinel Auth', description: 'Zero-trust authentication and authorization service', type: 'Security', status: 'active', priority: 'critical', progress: 81, health: 'healthy',
    activeAgents: 4, queuedItems: 6, activeItems: 5, completedItems: 38, blockedItems: 0, totalItems: 49, createdAt: '2026-01-15T09:00:00Z', updatedAt: '2026-03-16T09:00:00Z',
    repoUrl: 'https://github.com/auth0/node-jsonwebtoken',
  },
  {
    id: 'proj-4', name: 'Pulse Notifications', description: 'Multi-channel notification engine with smart delivery', type: 'Infrastructure', status: 'active', priority: 'medium', progress: 35, health: 'healthy',
    activeAgents: 3, queuedItems: 18, activeItems: 6, completedItems: 15, blockedItems: 1, totalItems: 40, createdAt: '2026-02-20T11:00:00Z', updatedAt: '2026-03-15T22:00:00Z',
    repoUrl: 'https://github.com/nodemailer/nodemailer',
  },
  {
    id: 'proj-5', name: 'Vortex Data Pipeline', description: 'High-throughput ETL pipeline with schema evolution', type: 'Data', status: 'planning', priority: 'high', progress: 12, health: 'healthy',
    activeAgents: 2, queuedItems: 30, activeItems: 3, completedItems: 5, blockedItems: 0, totalItems: 38, createdAt: '2026-03-05T08:00:00Z', updatedAt: '2026-03-16T06:00:00Z',
    repoUrl: 'https://github.com/brianc/node-postgres',
  },
  {
    id: 'proj-6', name: 'Horizon Mobile SDK', description: 'Cross-platform mobile SDK for partner integrations', type: 'SDK', status: 'paused', priority: 'low', progress: 55, health: 'at_risk',
    activeAgents: 0, queuedItems: 12, activeItems: 0, completedItems: 22, blockedItems: 3, totalItems: 37, createdAt: '2026-01-20T13:00:00Z', updatedAt: '2026-03-10T16:00:00Z',
    repoUrl: 'https://github.com/nicehash/NiceHashQuickMiner',
  },
];

const agentNames: Record<AgentRole, string[]> = {
  project_manager: ['Atlas PM', 'Meridian PM'],
  product_owner: ['Compass PO', 'Beacon PO'],
  architect: ['Blueprint Arch', 'Vertex Arch'],
  backend_engineer: ['Forge BE', 'Anvil BE', 'Crucible BE'],
  frontend_engineer: ['Prism FE', 'Pixel FE', 'Canvas FE'],
  devops_engineer: ['Pipeline DevOps', 'Deploy DevOps'],
  qa_engineer: ['Sentinel QA', 'Watchdog QA'],
  code_reviewer: ['Lens Reviewer', 'Scope Reviewer'],
  scheduler: ['Chronos Scheduler'],
  documentation: ['Scribe Docs'],
};

const roleLabels: Record<AgentRole, string> = {
  project_manager: 'Project Manager',
  product_owner: 'Product Owner',
  architect: 'Software Architect',
  backend_engineer: 'Backend Engineer',
  frontend_engineer: 'Frontend Engineer',
  devops_engineer: 'DevOps Engineer',
  qa_engineer: 'QA Engineer',
  code_reviewer: 'Code Reviewer',
  scheduler: 'Scheduler',
  documentation: 'Documentation',
};

export { roleLabels };

let agentId = 0;
export const agents: Agent[] = (Object.entries(agentNames) as [AgentRole, string[]][]).flatMap(([role, names]) =>
  names.map((name): Agent => {
    agentId++;
    const statuses: AgentStatus[] = ['active', 'active', 'active', 'idle', 'waiting', 'active'];
    const status = statuses[agentId % statuses.length];
    const proj = projects[agentId % projects.length];
    return {
      id: `agent-${agentId}`,
      name,
      role,
      specialty: roleLabels[role],
      status,
      currentTaskId: status === 'active' ? `task-${agentId * 10}` : undefined,
      currentTaskTitle: status === 'active' ? `Implement ${name.split(' ')[0].toLowerCase()} module` : undefined,
      currentProjectId: status !== 'idle' ? proj.id : undefined,
      currentProjectName: status !== 'idle' ? proj.name : undefined,
      utilization: status === 'active' ? 60 + Math.floor(Math.random() * 35) : status === 'idle' ? 0 : 20 + Math.floor(Math.random() * 30),
      lastHeartbeat: new Date(Date.now() - Math.floor(Math.random() * 60000)).toISOString(),
      queueDepth: Math.floor(Math.random() * 8),
      handoffCount: 10 + Math.floor(Math.random() * 50),
      successRate: 85 + Math.floor(Math.random() * 15),
      tasksCompleted: 20 + Math.floor(Math.random() * 100),
    };
  })
);

const eventTemplates: { type: ActivityEvent['type']; msgTemplate: string }[] = [
  { type: 'created', msgTemplate: '{agent} created epic "{entity}" in {project}' },
  { type: 'claimed', msgTemplate: '{agent} claimed task "{entity}" in {project}' },
  { type: 'completed', msgTemplate: '{agent} completed story "{entity}" in {project}' },
  { type: 'handed_off', msgTemplate: '{agent} handed off "{entity}" to next agent in {project}' },
  { type: 'reviewed', msgTemplate: '{agent} reviewed code for "{entity}" in {project}' },
  { type: 'blocked', msgTemplate: '{agent} flagged "{entity}" as blocked in {project}' },
  { type: 'deployed', msgTemplate: '{agent} deployed "{entity}" to staging in {project}' },
  { type: 'reprioritized', msgTemplate: '{agent} reprioritized queue for {project}' },
];

const entityNames = ['User Authentication Flow', 'API Gateway Setup', 'Dashboard Charts', 'WebSocket Integration', 'Search Indexer', 'Rate Limiting Module', 'Email Templates', 'CI/CD Pipeline', 'Database Migration', 'Error Tracking', 'Cache Layer', 'Payment Integration', 'Notification System', 'Role-Based Access', 'Data Export Feature'];

export const activityEvents: ActivityEvent[] = Array.from({ length: 40 }, (_, i) => {
  const template = eventTemplates[i % eventTemplates.length];
  const agent = agents[i % agents.length];
  const proj = projects[i % projects.length];
  const entity = entityNames[i % entityNames.length];
  return {
    id: `evt-${i + 1}`,
    type: template.type,
    actorAgentId: agent.id,
    actorAgentName: agent.name,
    actorRole: agent.role,
    relatedEntityType: (['epic', 'feature', 'story', 'task'] as const)[i % 4],
    relatedEntityId: `item-${i}`,
    relatedEntityTitle: entity,
    projectName: proj.name,
    timestamp: new Date(Date.now() - i * 180000).toISOString(),
    message: template.msgTemplate.replace('{agent}', agent.name).replace('{entity}', entity).replace('{project}', proj.name),
  };
});

const queueStatuses: WorkItemStatus[] = ['intake', 'breakdown', 'architecture', 'story_writing', 'task_generation', 'in_progress', 'code_review', 'qa', 'done', 'blocked'];
const priorities: Priority[] = ['critical', 'high', 'medium', 'low'];

export const queueItems: QueueItem[] = Array.from({ length: 35 }, (_, i) => ({
  id: `qi-${i + 1}`,
  title: entityNames[i % entityNames.length],
  type: (['epic', 'feature', 'story', 'task'] as const)[i % 4],
  projectName: projects[i % projects.length].name,
  assignedAgent: i % 3 === 0 ? agents[i % agents.length].name : undefined,
  priority: priorities[i % 4],
  status: queueStatuses[i % queueStatuses.length],
  createdAt: new Date(Date.now() - i * 3600000).toISOString(),
}));

export const projectEpics: Record<string, Epic[]> = {
  'proj-1': [
    {
      id: 'epic-1', projectId: 'proj-1', title: 'Core Platform Infrastructure', description: 'Foundation services and infrastructure setup', status: 'in_progress', progress: 75,
      features: [
        { id: 'feat-1', epicId: 'epic-1', title: 'Service Mesh Configuration', status: 'done', stories: [
          { id: 'story-1', featureId: 'feat-1', title: 'Setup Istio configuration', acceptanceCriteria: ['Istio installed', 'mTLS enabled'], status: 'done', assignedAgentId: 'agent-6', tasks: [
            { id: 'task-1', storyId: 'story-1', title: 'Install Istio operator', status: 'done', assignedAgentId: 'agent-6', estimate: 3, startedAt: '2026-03-10T10:00:00Z', completedAt: '2026-03-10T13:00:00Z' },
            { id: 'task-2', storyId: 'story-1', title: 'Configure mTLS policies', status: 'done', assignedAgentId: 'agent-6', estimate: 2 },
          ]},
        ]},
        { id: 'feat-2', epicId: 'epic-1', title: 'API Gateway', status: 'in_progress', stories: [
          { id: 'story-2', featureId: 'feat-2', title: 'Implement rate limiting', acceptanceCriteria: ['Rate limits configurable', 'Throttling works'], status: 'in_progress', assignedAgentId: 'agent-4', tasks: [
            { id: 'task-3', storyId: 'story-2', title: 'Design rate limit schema', status: 'done', assignedAgentId: 'agent-3', estimate: 2 },
            { id: 'task-4', storyId: 'story-2', title: 'Implement token bucket algorithm', status: 'in_progress', assignedAgentId: 'agent-4', estimate: 5 },
          ]},
          { id: 'story-3', featureId: 'feat-2', title: 'Add request routing', acceptanceCriteria: ['Path-based routing', 'Header-based routing'], status: 'backlog', tasks: [] },
        ]},
      ],
    },
    {
      id: 'epic-2', projectId: 'proj-1', title: 'Authentication & Authorization', description: 'User auth and RBAC implementation', status: 'in_progress', progress: 50,
      features: [
        { id: 'feat-3', epicId: 'epic-2', title: 'OAuth2 Integration', status: 'in_progress', stories: [
          { id: 'story-4', featureId: 'feat-3', title: 'Implement OAuth2 flows', acceptanceCriteria: ['Auth code flow works', 'Token refresh works'], status: 'code_review', assignedAgentId: 'agent-9', tasks: [
            { id: 'task-5', storyId: 'story-4', title: 'Build auth code exchange', status: 'done', assignedAgentId: 'agent-5', estimate: 4 },
            { id: 'task-6', storyId: 'story-4', title: 'Implement token refresh', status: 'code_review', assignedAgentId: 'agent-9', estimate: 3 },
          ]},
        ]},
      ],
    },
  ],
};

// Dashboard stats
export const dashboardStats = {
  totalProjects: projects.length,
  activeProjects: projects.filter(p => p.status === 'active').length,
  totalQueuedItems: projects.reduce((s, p) => s + p.queuedItems, 0),
  activeWorkItems: projects.reduce((s, p) => s + p.activeItems, 0),
  completedItems: projects.reduce((s, p) => s + p.completedItems, 0),
  blockedItems: projects.reduce((s, p) => s + p.blockedItems, 0),
  activeAgents: agents.filter(a => a.status === 'active').length,
  totalAgents: agents.length,
  avgUtilization: Math.round(agents.reduce((s, a) => s + a.utilization, 0) / agents.length),
};

export const throughputData = [
  { day: 'Mon', completed: 12, created: 18 },
  { day: 'Tue', completed: 19, created: 15 },
  { day: 'Wed', completed: 15, created: 22 },
  { day: 'Thu', completed: 24, created: 17 },
  { day: 'Fri', completed: 20, created: 14 },
  { day: 'Sat', completed: 8, created: 6 },
  { day: 'Sun', completed: 5, created: 9 },
];

// ─── HANDOFF EVENTS ───
const handoffTemplates = [
  { fromRole: 'project_manager' as AgentRole, toRole: 'architect' as AgentRole, desc: 'Feature decomposition' },
  { fromRole: 'architect' as AgentRole, toRole: 'product_owner' as AgentRole, desc: 'Story package ready' },
  { fromRole: 'product_owner' as AgentRole, toRole: 'backend_engineer' as AgentRole, desc: 'Task bundle created' },
  { fromRole: 'backend_engineer' as AgentRole, toRole: 'code_reviewer' as AgentRole, desc: 'PR submitted' },
  { fromRole: 'code_reviewer' as AgentRole, toRole: 'qa_engineer' as AgentRole, desc: 'Approved for validation' },
  { fromRole: 'frontend_engineer' as AgentRole, toRole: 'code_reviewer' as AgentRole, desc: 'UI component PR ready' },
  { fromRole: 'qa_engineer' as AgentRole, toRole: 'devops_engineer' as AgentRole, desc: 'QA passed, deploy ready' },
  { fromRole: 'devops_engineer' as AgentRole, toRole: 'project_manager' as AgentRole, desc: 'Deployment complete' },
];

function findAgentByRole(role: AgentRole, offset: number): Agent {
  const matching = agents.filter(a => a.role === role);
  return matching[offset % matching.length] || agents[0];
}

export const handoffEvents: HandoffEvent[] = Array.from({ length: 24 }, (_, i) => {
  const template = handoffTemplates[i % handoffTemplates.length];
  const fromAgent = findAgentByRole(template.fromRole, i);
  const toAgent = findAgentByRole(template.toRole, i + 1);
  const proj = projects[i % projects.length];
  const statuses: HandoffEvent['status'][] = ['completed', 'completed', 'completed', 'pending', 'completed', 'rejected'];
  return {
    id: `hoff-${i + 1}`,
    fromAgentId: fromAgent.id,
    fromAgentName: fromAgent.name,
    fromRole: fromAgent.role,
    toAgentId: toAgent.id,
    toAgentName: toAgent.name,
    toRole: toAgent.role,
    workItemTitle: entityNames[i % entityNames.length],
    workItemType: (['feature', 'story', 'task', 'story'] as const)[i % 4],
    projectName: proj.name,
    timestamp: new Date(Date.now() - i * 420000).toISOString(),
    status: statuses[i % statuses.length],
  };
});

// ─── SYSTEM METRICS ───
export const systemMetrics: SystemMetrics = {
  healthScore: 87,
  throughputScore: 82,
  blockedRatio: 4.2,
  velocityTrend: 12,
  reviewTurnaround: 2.4,
  testingPassRate: 94,
  agentResponseTime: 1.8,
  completionVelocity: 18.3,
};

// ─── THROUGHPUT HEATMAP ───
const heatmapStages = ['Intake', 'Breakdown', 'Architecture', 'Story Writing', 'Coding', 'Review', 'QA', 'Deploy'];
const heatmapPeriods = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export const throughputHeatmap: ThroughputCell[] = heatmapStages.flatMap((stage, si) =>
  heatmapPeriods.map((period, pi) => ({
    stage,
    period,
    value: Math.max(0, Math.round(
      (stage === 'Coding' || stage === 'Review' ? 14 : 8) *
      (pi < 5 ? 1.2 : 0.4) *
      (0.3 + Math.sin(si * 0.8 + pi * 1.2) * 0.5 + Math.random() * 0.4)
    )),
  }))
);

// ─── BOTTLENECK ITEMS ───
export const bottleneckItems: BottleneckItem[] = [
  {
    id: 'bn-1', severity: 'critical', category: 'Pipeline Congestion',
    affectedProject: 'Quantum Analytics', affectedStage: 'Code Review',
    description: '14 PRs waiting for review with avg 6h wait time',
    suggestedAction: 'Redistribute review load or add reviewer agent',
  },
  {
    id: 'bn-2', severity: 'high', category: 'Blocked Dependencies',
    affectedProject: 'Nexus Platform', affectedStage: 'In Progress',
    description: '3 tasks blocked by unresolved API integration dependency',
    suggestedAction: 'Escalate API contract resolution with Architect agent',
  },
  {
    id: 'bn-3', severity: 'high', category: 'QA Backlog',
    affectedProject: 'Sentinel Auth', affectedStage: 'QA',
    description: 'Testing queue depth growing — 8 stories awaiting validation',
    suggestedAction: 'Provision additional QA agent or prioritize critical paths',
  },
  {
    id: 'bn-4', severity: 'medium', category: 'Agent Overload',
    affectedProject: 'Nexus Platform', affectedStage: 'Architecture',
    description: 'Blueprint Arch at 97% utilization with 6 items in queue',
    suggestedAction: 'Offload non-critical items to Vertex Arch',
  },
  {
    id: 'bn-5', severity: 'medium', category: 'Low Heartbeat',
    affectedProject: 'Horizon Mobile SDK', affectedStage: 'All',
    description: 'Deploy DevOps agent last heartbeat 45min ago',
    suggestedAction: 'Check agent health and restart if necessary',
  },
];
