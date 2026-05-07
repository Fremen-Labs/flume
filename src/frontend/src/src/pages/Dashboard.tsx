import { motion } from 'framer-motion';
import {
  Bot, ListTodo, CheckCircle2, AlertTriangle, Eye,
  TrendingUp, ArrowRight, Zap, Clock, Activity,
} from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { useTelemetry } from '@/hooks/useTelemetry';
import { StatusBadge } from '@/components/StatusBadge';
import { useNavigate } from 'react-router-dom';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import projBg1 from '@/assets/projects/proj-bg-1.jpg';
import projBg2 from '@/assets/projects/proj-bg-2.jpg';
import projBg3 from '@/assets/projects/proj-bg-3.jpg';
import agentAvatar1 from '@/assets/agents/agent-1.png';
import agentAvatar2 from '@/assets/agents/agent-2.png';
import agentAvatar3 from '@/assets/agents/agent-3.png';
import agentAvatar4 from '@/assets/agents/agent-4.png';

const agentAvatars = [agentAvatar1, agentAvatar2, agentAvatar3, agentAvatar4];
const projectBgs = [projBg1, projBg2, projBg3];

function timeAgo(ts?: string) {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function SectionHeader({ title }: { title: string }) {
  return (
    <div className="flex items-center gap-3 mb-4">
      <span className="text-xs text-muted-foreground">•</span>
      <h3 className="text-sm font-semibold text-foreground whitespace-nowrap">{title}</h3>
      <div className="flex-1 border-t border-dashed border-white/[0.06]" />
    </div>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const { data: snapshot, isLoading } = useSnapshot();
  const { data: telemetry } = useTelemetry();

  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];
  const projects = snapshot?.projects ?? [];
  const failures = snapshot?.failures ?? [];

  const activeWorkers = workers.filter(w => w.status === 'claimed' || w.status === 'active');
  const runningTasks = tasks.filter(t => t.status === 'running');
  const inReviewTasks = tasks.filter(t => t.status === 'review');
  const doneTasks = tasks.filter(t => t.status === 'done');
  const blockedTasks = tasks.filter(t => t.status === 'blocked');
  const plannedTasks = tasks.filter(t => t.status === 'planned' || t.status === 'ready' || t.status === 'inbox');

  // Derive recent activity from tasks sorted by last_update
  const recentActivity = [...tasks]
    .filter(t => t.last_update || t.updated_at)
    .sort((a, b) => new Date(b.last_update ?? b.updated_at ?? 0).getTime() - new Date(a.last_update ?? a.updated_at ?? 0).getTime())
    .slice(0, 6);

  const pipelineStages = [
    { label: 'Planned', count: plannedTasks.length, color: 'bg-muted-foreground', desc: 'Queued for agentic blueprint extraction natively.' },
    { label: 'Running', count: runningTasks.length, color: 'bg-primary', desc: 'Synchronous execution across parallel orchestrator nodes.' },
    { label: 'In Review', count: inReviewTasks.length, color: 'bg-violet-500', desc: 'Tasks awaiting tester or reviewer evaluation before completion.' },
    { label: 'Done', count: doneTasks.length, color: 'bg-success', desc: 'Verified and merged safely bypassing evaluation structures.' },
    { label: 'Blocked', count: blockedTasks.length, color: 'bg-destructive', desc: 'Execution exceptions requiring structural AST interventions.' },
  ];

  return (
    <div className="p-5 lg:p-6 max-w-[1600px] mx-auto space-y-5 relative">

      {/* Header Bar */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="flex items-center justify-between relative z-10">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-primary/15 flex items-center justify-center breathing">
            <Bot className="w-5 h-5 text-primary icon-glow-active" />
          </div>
          <h1 className="text-lg font-bold tracking-tight text-foreground">AI Project Command Center</h1>
        </div>
        <div className="flex items-center gap-3">
          <div className="glass-card px-3 py-1.5 flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${activeWorkers.length > 0 ? 'bg-success' : 'bg-muted-foreground'} opacity-75`} />
              <span className={`relative inline-flex rounded-full h-2 w-2 ${activeWorkers.length > 0 ? 'bg-success' : 'bg-muted-foreground'}`} />
            </span>
            <span className="text-foreground font-medium">{activeWorkers.length}</span>
            <span className="text-muted-foreground">Active Agents</span>
          </div>
          <div className="glass-card px-3 py-1.5 flex items-center gap-2 text-xs">
            <AlertTriangle className="w-3 h-3 text-destructive" />
            <span className="text-foreground font-medium">{blockedTasks.length}</span>
            <span className="text-muted-foreground">Blocked Items</span>
          </div>
        </div>
      </motion.div>

      {/* Top Stats Bar */}
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 }} className="glass-card-sweep p-4 relative z-10">
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-6 text-sm">
            <div><span className="text-2xl font-bold text-foreground">{projects.length}</span> <span className="text-muted-foreground text-xs">Total Projects</span></div>
            <div className="w-px h-6 bg-white/[0.06]" />
            <div><span className="text-2xl font-bold text-primary">{workers.length}</span> <span className="text-muted-foreground text-xs">Workers</span></div>
            <div className="w-px h-6 bg-white/[0.06]" />
            <div><span className="text-2xl font-bold text-foreground">{plannedTasks.length}</span> <span className="text-muted-foreground text-xs">In Queue</span></div>
            <div className="w-px h-6 bg-white/[0.06]" />
            <div><span className="text-2xl font-bold text-foreground">{doneTasks.length}</span> <span className="text-muted-foreground text-xs">Completed</span></div>
          </div>
          <div className="text-xs text-muted-foreground flex items-center gap-1">
            {isLoading ? 'Loading…' : 'Live data'}
            <TrendingUp className="w-3 h-3" />
          </div>
        </div>
      </motion.div>

      {/* Metric Cards */}
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="grid grid-cols-2 md:grid-cols-5 gap-4 relative z-10">
        {[
          { title: 'Tasks Running', value: runningTasks.length, icon: Zap, color: 'text-primary', description: "Real-time AI compute clusters generating code currently synced to parallel Git worktrees." },
          { title: 'In Review', value: inReviewTasks.length, icon: Eye, color: 'text-violet-400', description: "Tasks awaiting automated tester validation or reviewer evaluation before final approval." },
          { title: 'Tasks in Queue', value: plannedTasks.length, icon: ListTodo, color: 'text-muted-foreground', description: "Tickets staged by the orchestrator awaiting native daemon resource allocation bounds." },
          { title: 'Tasks Completed', value: doneTasks.length, icon: CheckCircle2, color: 'text-success', description: "System verified pipelines pushed natively successfully bypassing the PR Critic thresholds." },
          { title: 'Blocked Issues', value: blockedTasks.length, icon: AlertTriangle, color: 'text-destructive', description: blockedTasks.length > 0 ? (
            <div className="space-y-2 mt-1">
              <span className="block mb-2">Tasks flagged for explicit human intervention resolving Structural AST exceptions or loops.</span>
              {blockedTasks.slice(0, 3).map(t => (
                <div key={t.id} className="text-[10px] bg-black/40 p-1.5 rounded text-white/80 leading-snug">
                  <span className="text-destructive font-medium">{t.id}:</span> {t.message || 'Blocked without explicit reason'}
                </div>
              ))}
              {blockedTasks.length > 3 && <div className="text-[10px] text-muted-foreground">+{blockedTasks.length - 3} more</div>}
            </div>
          ) : "Tasks flagged for explicit human intervention resolving Structural AST exceptions or loops." },
        ].map(card => (
          <Tooltip key={card.title} delayDuration={150}>
            <TooltipTrigger asChild>
              <motion.div
                whileHover={{ y: -3, transition: { duration: 0.25 } }}
                whileTap={{ scale: 0.98 }}
                onClick={() => navigate('/queue')}
                className="glass-card hover-lift p-4 flex items-center gap-3 cursor-pointer group"
              >
                <div className="p-2 rounded-lg bg-primary/10 flex-shrink-0 group-hover:bg-primary/20 transition-colors">
                  <card.icon className="w-4 h-4 text-primary group-hover:scale-110 transition-transform duration-300" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-[10px] text-muted-foreground">{card.title}</p>
                  <span className={`text-2xl font-bold ${card.color}`}>{card.value}</span>
                </div>
              </motion.div>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="max-w-[240px] text-xs leading-relaxed glass-panel border-white/[0.1] shadow-2xl p-3">
              <div className="text-foreground/90">{card.description}</div>
              <div className="mt-2 text-[10px] text-primary flex items-center gap-1 font-medium">
                Click to view details <ArrowRight className="w-3 h-3" />
              </div>
            </TooltipContent>
          </Tooltip>
        ))}
      </motion.div>

      {/* Main layout */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-5 relative z-10">
        {/* Left (3 cols) */}
        <div className="lg:col-span-3 space-y-5">
          {/* Projects */}
          <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }}>
            <SectionHeader title="Projects" />
            {projects.length === 0 ? (
              <div className="glass-card p-8 text-center text-sm text-muted-foreground">
                No projects yet.
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {projects.slice(0, 3).map((project, idx) => {
                  const ptasks = tasks.filter(t => t.repo === project.id);
                  const pDone = ptasks.filter(t => t.status === 'done').length;
                  const pRunning = ptasks.filter(t => t.status === 'running').length;
                  const pActive = workers.filter(w => w.current_task_id && ptasks.some(t => t.id === w.current_task_id)).length;
                  return (
                    <motion.div
                      key={project.id}
                      whileHover={{ y: -4, transition: { duration: 0.25 } }}
                      className="glass-card-glow cursor-pointer group relative overflow-hidden hover-lift"
                      onClick={() => navigate(`/projects/${encodeURIComponent(project.id)}`)}
                    >
                      <div className="absolute inset-0">
                        <img src={projectBgs[idx % projectBgs.length]} alt="" className="w-full h-full object-cover opacity-15 group-hover:opacity-25 transition-opacity duration-700" />
                        <div className="absolute inset-0 bg-gradient-to-t from-card via-card/85 to-card/30" />
                      </div>
                      <div className="relative p-5 z-[3]">
                        <h4 className="text-sm font-semibold text-foreground group-hover:text-primary transition-colors truncate">{project.name}</h4>
                        <p className="text-[10px] text-muted-foreground mt-0.5 truncate">{project.repoUrl || project.path}</p>
                        <div className="flex items-center gap-4 text-[10px] text-muted-foreground mt-3">
                          <span className="flex items-center gap-1"><Bot className="w-3 h-3" /> {pActive} agents</span>
                          <span className="flex items-center gap-1 text-primary"><Activity className="w-3 h-3" /> {pRunning} running</span>
                          <span className="flex items-center gap-1 text-success"><CheckCircle2 className="w-3 h-3" /> {pDone} done</span>
                        </div>
                      </div>
                    </motion.div>
                  );
                })}
              </div>
            )}
          </motion.div>

          {/* Pipeline */}
          <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }}>
            <SectionHeader title="Work Pipeline" />
            <div className="glass-panel p-5">
              <div className="flex items-center justify-between relative z-10">
                {pipelineStages.map((stage, i) => (
                  <Tooltip key={stage.label} delayDuration={150}>
                    <TooltipTrigger asChild>
                      <motion.div whileHover={{ y: -2, scale: 1.02 }} className="flex items-center flex-1 cursor-help group">
                        <div className="flex-1 text-center">
                          <div className="flex items-center justify-center gap-1.5 mb-2">
                            <span className={`w-2 h-2 rounded-full ${stage.color} group-hover:shadow-[0_0_8px_currentColor] transition-shadow duration-300`} />
                            <span className="text-xs font-medium text-foreground">{stage.label}</span>
                          </div>
                          <div className="text-2xl font-bold text-foreground group-hover:text-primary transition-colors">{stage.count}</div>
                        </div>
                        {i < pipelineStages.length - 1 && (
                          <div className="flex items-center gap-1 px-2 text-muted-foreground/30">
                            <div className="w-1 h-1 rounded-full bg-white/10" />
                            <div className="w-1 h-1 rounded-full bg-white/10" />
                            <div className="w-1 h-1 rounded-full bg-white/10" />
                            <ArrowRight className="w-3 h-3 text-muted-foreground/30" />
                          </div>
                        )}
                      </motion.div>
                    </TooltipTrigger>
                    <TooltipContent side="top" className="max-w-[200px] text-xs leading-relaxed glass-panel border-white/[0.1] shadow-2xl p-2.5">
                      <p className="text-foreground/90">{stage.desc}</p>
                    </TooltipContent>
                  </Tooltip>
                ))}
              </div>
            </div>
          </motion.div>

          {/* Recent Activity */}
          <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }}>
            <SectionHeader title="Recent Activity" />
            <div className="glass-card p-5">
              <div className="space-y-3 relative z-10">
                {recentActivity.length === 0 && (
                  <p className="text-sm text-muted-foreground text-center py-4">No recent activity.</p>
                )}
                {recentActivity.map((task, i) => (
                  <motion.div
                    key={task._id}
                    initial={{ opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.3 + i * 0.05 }}
                    className="flex items-center gap-3 py-2 border-b border-white/[0.04] last:border-b-0 px-2"
                  >
                    <div className="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center text-xs flex-shrink-0">
                      <Clock className="w-4 h-4 text-primary/60" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs text-foreground/90 truncate">{task.title}</p>
                      <div className="flex items-center gap-2 mt-0.5">
                        <StatusBadge status={task.status} />
                        <span className="text-[10px] text-muted-foreground">{task.repo}</span>
                      </div>
                    </div>
                    <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                      {timeAgo(task.last_update ?? task.updated_at)}
                    </span>
                  </motion.div>
                ))}
              </div>
            </div>
          </motion.div>

          {/* Failures */}
          {failures.length > 0 && (
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.3 }}>
              <SectionHeader title="Recent Failures" />
              <div className="glass-card p-5 border border-destructive/20">
                <div className="space-y-3">
                  {failures.slice(0, 3).map((f, i) => (
                    <div key={f._id ?? i} className="flex items-start gap-3 py-2 border-b border-white/[0.04] last:border-b-0">
                      <AlertTriangle className="w-4 h-4 text-destructive shrink-0 mt-0.5" />
                      <div className="flex-1 min-w-0">
                        <p className="text-xs text-foreground/90 font-medium">{f.error_class}</p>
                        <p className="text-[10px] text-muted-foreground mt-0.5 truncate">{f.summary}</p>
                        <p className="text-[10px] text-muted-foreground/50 mt-0.5">task: {f.task_id}</p>
                      </div>
                      <span className="text-[10px] text-muted-foreground whitespace-nowrap">{timeAgo(f.created_at)}</span>
                    </div>
                  ))}
                </div>
              </div>
            </motion.div>
          )}
        </div>

        {/* Right sidebar (1 col) */}
        <div className="space-y-5">
          {/* Active Workers */}
          <motion.div initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.2 }}>
            <SectionHeader title="Active Workers" />
            <div className="space-y-2">
              {workers.length === 0 && (
                <div className="glass-card p-4 text-center text-xs text-muted-foreground">No workers running.</div>
              )}
              {workers.map((worker, i) => {
                const isActive = worker.status === 'claimed' || worker.status === 'active';
                return (
                  <motion.div
                    key={worker.name}
                    initial={{ opacity: 0, x: 8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: 0.2 + i * 0.05 }}
                    className={`glass-card p-3 ${isActive ? 'border border-primary/10' : ''}`}
                  >
                    <div className="flex items-center gap-2.5">
                      <div className="w-7 h-7 rounded-full overflow-hidden ring-1 ring-primary/20 shrink-0">
                        <img src={agentAvatars[i % agentAvatars.length]} alt="" className="w-full h-full object-cover" />
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center justify-between">
                          <p className="text-xs font-medium text-foreground truncate">{worker.name}</p>
                          <StatusBadge status={worker.status} pulse />
                        </div>
                        {worker.current_task_title && (
                          <p className="text-[10px] text-muted-foreground truncate">{worker.current_task_title}</p>
                        )}
                      </div>
                    </div>
                  </motion.div>
                );
              })}
            </div>
          </motion.div>

          {/* Local Intelligence */}
          <motion.div initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.25 }}>
            <SectionHeader title="Local Intelligence" />
            <div className="glass-card p-4 space-y-4">
              <div className="flex justify-between items-center text-sm">
                <span className="text-muted-foreground flex items-center gap-2">
                  <AlertTriangle className="w-3.5 h-3.5 text-orange-400" /> Mesh Throttling
                </span>
                <span className="font-semibold text-foreground">
                  {telemetry?.flume_concurrency_throttled_total || 0}
                </span>
              </div>
              <div className="flex justify-between items-center text-sm hover:bg-white/[0.02] -mx-2 px-2 py-1 rounded transition-colors cursor-pointer" onClick={() => navigate('/queue')}>
                <span className="text-muted-foreground flex items-center gap-2">
                  <Activity className="w-3.5 h-3.5 text-destructive" /> Total Blocked
                </span>
                <span className="font-semibold text-foreground">
                  {telemetry?.flume_tasks_blocked_total || blockedTasks.length}
                </span>
              </div>
            </div>
          </motion.div>
        </div>
      </div>
    </div>
  );
}
