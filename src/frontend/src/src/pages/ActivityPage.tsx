import { motion } from 'framer-motion';
import { Loader2, Clock } from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';

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

const statusIcon: Record<string, string> = {
  done: '✅', running: '🤖', blocked: '🚫', planned: '📋', ready: '🚀', failed: '❌',
};

export default function ActivityPage() {
  const { data: snapshot, isLoading } = useSnapshot();

  const tasks = [...(snapshot?.tasks ?? [])]
    .filter(t => t.last_update || t.updated_at)
    .sort((a, b) =>
      new Date(b.last_update ?? b.updated_at ?? 0).getTime() -
      new Date(a.last_update ?? a.updated_at ?? 0).getTime(),
    );

  const failures = snapshot?.failures ?? [];
  const reviews = snapshot?.reviews ?? [];

  return (
    <div className="p-6 lg:p-8 max-w-[1000px] mx-auto space-y-6 relative">

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Activity Feed</h1>
        <p className="text-sm text-muted-foreground mt-1">System-wide audit trail — {tasks.length} tasks</p>
      </motion.div>

      {isLoading && (
        <div className="flex items-center gap-2 text-muted-foreground py-10">
          <Loader2 className="w-4 h-4 animate-spin" />Loading…
        </div>
      )}

      {!isLoading && (
        <div className="relative z-10">
          {/* Timeline line */}
          <div className="absolute left-[23px] top-0 bottom-0 w-px bg-white/[0.05]" />

          <div className="space-y-3">
            {tasks.length === 0 && (
              <div className="glass-card p-8 text-center text-sm text-muted-foreground">
                No activity yet.
              </div>
            )}
            {tasks.map((task, i) => (
              <motion.div
                key={task._id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.02 }}
                className="flex items-start gap-4 pl-2"
              >
                {/* Icon */}
                <div className="w-9 h-9 rounded-full bg-card border border-border flex items-center justify-center text-sm flex-shrink-0 z-10">
                  {statusIcon[task.status] ?? <Clock className="w-4 h-4 text-muted-foreground" />}
                </div>

                <div className="flex-1 glass-card p-4 min-w-0">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium text-foreground truncate">{task.title}</p>
                      {task.objective && (
                        <p className="text-[10px] text-muted-foreground mt-0.5 truncate">{task.objective}</p>
                      )}
                    </div>
                    <span className="text-[10px] text-muted-foreground whitespace-nowrap">
                      {timeAgo(task.last_update ?? task.updated_at)}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    <StatusBadge status={task.status} />
                    <span className="text-[10px] px-2 py-0.5 rounded-full glass-surface text-muted-foreground">{task.repo}</span>
                    {task.item_type && (
                      <span className="text-[10px] px-2 py-0.5 rounded-full glass-surface text-muted-foreground">
                        {task.item_type}
                      </span>
                    )}
                    {task.branch && (
                      <code className="text-[10px] text-primary/70">{task.branch}</code>
                    )}
                  </div>
                </div>
              </motion.div>
            ))}

            {/* Reviews */}
            {reviews.length > 0 && (
              <>
                <div className="text-xs font-semibold text-muted-foreground py-2 pl-12">Reviews</div>
                {reviews.map((r, i) => (
                  <motion.div
                    key={r._id}
                    initial={{ opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: tasks.length * 0.02 + i * 0.02 }}
                    className="flex items-start gap-4 pl-2"
                  >
                    <div className="w-9 h-9 rounded-full bg-card border border-border flex items-center justify-center text-sm flex-shrink-0 z-10">
                      {r.verdict === 'approved' ? '✅' : '🔍'}
                    </div>
                    <div className="flex-1 glass-card p-4 min-w-0">
                      <div className="flex items-start justify-between gap-2">
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-foreground">Review: {r.task_id}</p>
                          <p className="text-[10px] text-muted-foreground mt-0.5 truncate">{r.summary}</p>
                        </div>
                        <span className="text-[10px] text-muted-foreground whitespace-nowrap">{timeAgo(r.created_at)}</span>
                      </div>
                      <div className="flex items-center gap-2 mt-2">
                        <StatusBadge status={r.verdict === 'approved' ? 'done' : 'running'} />
                        {r.model_used && (
                          <span className="text-[10px] text-muted-foreground">{r.model_used}</span>
                        )}
                      </div>
                    </div>
                  </motion.div>
                ))}
              </>
            )}

            {/* Failures */}
            {failures.length > 0 && (
              <>
                <div className="text-xs font-semibold text-destructive py-2 pl-12">Failures</div>
                {failures.map((f, i) => (
                  <motion.div
                    key={f._id}
                    initial={{ opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: (tasks.length + reviews.length) * 0.02 + i * 0.02 }}
                    className="flex items-start gap-4 pl-2"
                  >
                    <div className="w-9 h-9 rounded-full bg-card border border-destructive/30 flex items-center justify-center text-sm flex-shrink-0 z-10">❌</div>
                    <div className="flex-1 glass-card p-4 border border-destructive/20 min-w-0">
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-xs font-medium text-destructive truncate">{f.error_class}</p>
                        <span className="text-[10px] text-muted-foreground whitespace-nowrap">{timeAgo(f.created_at)}</span>
                      </div>
                      <p className="text-[10px] text-muted-foreground mt-1 truncate">{f.summary}</p>
                    </div>
                  </motion.div>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
