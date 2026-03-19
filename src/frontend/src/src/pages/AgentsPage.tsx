import { motion } from 'framer-motion';
import { Bot, Loader2, AlertCircle } from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';
import agentAvatar1 from '@/assets/agents/agent-1.png';
import agentAvatar2 from '@/assets/agents/agent-2.png';
import agentAvatar3 from '@/assets/agents/agent-3.png';
import agentAvatar4 from '@/assets/agents/agent-4.png';

const agentAvatars = [agentAvatar1, agentAvatar2, agentAvatar3, agentAvatar4];

const roleLabels: Record<string, string> = {
  intake: 'Intake Agent',
  pm: 'Project Manager',
  implementer: 'Implementer',
  tester: 'Tester',
  reviewer: 'Code Reviewer',
  'memory-updater': 'Memory Updater',
  planner: 'Planner',
  architect: 'Architect',
  devops: 'DevOps',
  qa: 'QA Engineer',
};

function timeAgo(ts?: string) {
  if (!ts) return 'unknown';
  const diff = Date.now() - new Date(ts).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

export default function AgentsPage() {
  const { data: snapshot, isLoading, error } = useSnapshot();
  const workers = snapshot?.workers ?? [];

  const activeCount = workers.filter(w => w.status === 'claimed' || w.status === 'active').length;
  const idleCount = workers.filter(w => w.status === 'idle').length;

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Agent Operations</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {isLoading
            ? 'Loading…'
            : `${activeCount} active · ${idleCount} idle · ${workers.length} total`}
        </p>
      </motion.div>

      {isLoading && (
        <div className="flex items-center justify-center py-20 gap-3 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="text-sm">Connecting to workers…</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-3 p-4 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          Failed to connect to backend.
        </div>
      )}

      {!isLoading && !error && workers.length === 0 && (
        <div className="glass-card p-12 text-center text-sm text-muted-foreground">
          <Bot className="w-8 h-8 mx-auto mb-3 opacity-30" />
          No workers running. Start the worker manager to see agents here.
        </div>
      )}

      {/* Agent Grid */}
      {!isLoading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 relative z-10">
          {workers.map((worker, i) => {
            const isActive = worker.status === 'claimed' || worker.status === 'active';
            return (
              <motion.div
                key={worker.name}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.03 }}
                whileHover={{ y: -3, transition: { duration: 0.2 } }}
                className={`glass-card-glow p-5 hover-lift relative ${isActive ? 'agent-breathing' : ''}`}
              >
                <div className="flex items-start justify-between mb-3 relative z-10">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-full overflow-hidden ring-1 ring-primary/20">
                      <img src={agentAvatars[i % agentAvatars.length]} alt={worker.name} className="w-full h-full object-cover" />
                    </div>
                    <div>
                      <h3 className="text-sm font-semibold text-foreground">{worker.name}</h3>
                      <p className="text-[10px] text-muted-foreground">{roleLabels[worker.role] ?? worker.role}</p>
                    </div>
                  </div>
                  <StatusBadge status={worker.status} pulse />
                </div>

                {worker.current_task_title && (
                  <div className="text-[11px] text-muted-foreground mb-3 relative z-10 bg-muted/10 rounded p-2 border border-border/30">
                    <span className="text-[10px] font-medium text-primary/80 block mb-0.5">Current Task</span>
                    {worker.current_task_title}
                  </div>
                )}

                <div className="grid grid-cols-2 gap-2 relative z-10">
                  <div className="glass-surface p-2 rounded text-center">
                    <div className="text-xs font-medium text-foreground">{worker.model ?? worker.preferred_model ?? '—'}</div>
                    <div className="text-[10px] text-muted-foreground">Model</div>
                  </div>
                  <div className="glass-surface p-2 rounded text-center">
                    <div className="text-xs font-medium text-foreground">{worker.execution_host ?? '—'}</div>
                    <div className="text-[10px] text-muted-foreground">Host</div>
                  </div>
                </div>

                <div className="mt-3 flex items-center justify-between text-[10px] text-muted-foreground relative z-10">
                  <span>Heartbeat: {timeAgo(worker.heartbeat_at)}</span>
                  {worker.current_task_id && (
                    <code className="text-primary/70">{worker.current_task_id}</code>
                  )}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}
