import { motion } from 'framer-motion';
import { useState } from 'react';
import { Loader2, AlertCircle, GitBranch, ShieldAlert } from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';
import { AgentThoughtDrawer } from '@/components/AgentThoughtDrawer';
import { useToast } from '@/hooks/use-toast';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { ApiTask } from '@/types';
import { appLogger } from '@/utils/logger';

const stages: { id: string; label: string }[] = [
  { id: 'inbox', label: 'Inbox' },
  { id: 'planned', label: 'Planned' },
  { id: 'ready', label: 'Ready' },
  { id: 'running', label: 'Running' },
  { id: 'review', label: 'In Review' },
  { id: 'done', label: 'Done' },
  { id: 'blocked', label: 'Blocked' },
];

const typeColors: Record<string, string> = {
  epic: 'bg-primary/10 text-primary',
  feature: 'bg-purple-500/10 text-purple-400',
  story: 'bg-cyan-500/10 text-cyan-400',
  task: 'bg-muted text-muted-foreground',
};

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

export default function QueuePage() {
  // `useSnapshot` wraps `useQuery`, which exposes `refetch` (not `mutate`).
  // Alias it so existing callsites keep working.
  const { data: snapshot, isLoading, error, refetch: mutate } = useSnapshot();
  const { toast } = useToast();
  const [isHalting, setIsHalting] = useState(false);
  const [dialogAction, setDialogAction] = useState<'halt' | 'resume' | null>(null);
  const [adminToken, setAdminToken] = useState(() => localStorage.getItem('flume-admin-token') || '');
  const [thoughtTaskId, setThoughtTaskId] = useState<string | null>(null);
  const [thoughtTaskTitle, setThoughtTaskTitle] = useState<string | undefined>(undefined);
  const [thoughtTaskStatus, setThoughtTaskStatus] = useState<string | undefined>(undefined);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [unblockTarget, setUnblockTarget] = useState<ApiTask | null>(null);
  const [unblockQueueNote, setUnblockQueueNote] = useState('');
  const [unblockQueueBusy, setUnblockQueueBusy] = useState(false);
  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];

  async function submitQueueUnblock() {
    if (!unblockTarget?.id) return;
    setUnblockQueueBusy(true);
    try {
      const body: Record<string, unknown> = { status: 'ready' };
      const ins = unblockQueueNote.trim();
      if (ins) body.instruction = ins;
      const r = await fetch(`/api/tasks/${encodeURIComponent(unblockTarget.id)}/transition`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data: { error?: string } = await r.json().catch(() => ({}));
      if (!r.ok) {
        throw new Error(data?.error || 'Unblock failed');
      }
      toast({
        title: 'Task unblocked',
        description: ins ? 'Guidance saved; task re-queued.' : 'Recovery hint added; task re-queued.',
      });
      setUnblockTarget(null);
      setUnblockQueueNote('');
      mutate();
    } catch (e) {
      toast({
        title: 'Unblock failed',
        description: e instanceof Error ? e.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      setUnblockQueueBusy(false);
    }
  }

  const handleConfirmAction = async () => {
    if (!dialogAction) return;
    try {
      setIsHalting(true);
      const url = dialogAction === 'halt' ? '/api/tasks/stop-all' : '/api/tasks/resume-all';
      const res = await fetch(url, { 
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${adminToken}`
        }
      });
      if (res.ok) {
        mutate();
        toast({ title: dialogAction === 'halt' ? "Swarms Halted" : "Swarms Resumed", description: dialogAction === 'halt' ? "All active tasks successfully halted." : "Blocked tasks restored successfully." });
      } else {
        const errorBody = await res.json().catch(() => ({}));
        const description = errorBody.error 
          ? `Error: ${errorBody.error} (Request ID: ${errorBody.correlation_id || 'Unknown'})`
          : "An unknown error occurred resolving the native API.";
        toast({ title: "Operation Failed", description, variant: "destructive" });
      }
    } catch (e) {
      appLogger.error('Swarms request failed:', e);
      toast({ title: "System Exception", description: "Exception occurred triggering Swarm operation bounds.", variant: "destructive" });
    } finally {
      setIsHalting(false);
      setDialogAction(null);
    }
  };

  return (
    <div className="p-6 lg:p-8 max-w-[1800px] mx-auto space-y-6 relative">

      <Dialog open={!!dialogAction} onOpenChange={(open) => !open && setDialogAction(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialogAction === 'halt' ? 'Stop All Swarms' : 'Resume Blocked Swarms'}</DialogTitle>
            <DialogDescription className="space-y-3 pt-2">
              <p>{dialogAction === 'halt' 
                ? 'Are you sure you want to stop all active tasks? This will immediately terminate running processes and block all queued tasks from starting.'
                : 'Are you sure you want to resume all halted tasks? This will reset them back into the active pipeline pool.'}</p>
              <Input 
                type="password" 
                placeholder="Flume Admin Token" 
                value={adminToken} 
                onChange={(e) => {
                  setAdminToken(e.target.value);
                  localStorage.setItem('flume-admin-token', e.target.value);
                }} 
                className="w-full"
              />
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-0 mt-4">
            <button
              onClick={() => setDialogAction(null)}
              className="px-4 py-2 border rounded-md text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirmAction}
              disabled={isHalting}
              className={`px-4 py-2 rounded-md text-sm flex items-center justify-center gap-2 text-primary-foreground ${dialogAction === 'halt' ? 'bg-destructive hover:bg-destructive/90 text-destructive-foreground' : 'bg-primary hover:bg-primary/90'}`}
            >
              {isHalting && <Loader2 className="w-4 h-4 animate-spin" />}
              {isHalting ? (dialogAction === 'halt' ? 'Terminating...' : 'Resuming...') : (dialogAction === 'halt' ? 'Force Kill Processes' : 'Resume Tasks')}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={unblockTarget !== null} onOpenChange={(open) => { if (!open) { setUnblockTarget(null); setUnblockQueueNote(''); } }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Unblock blocked task</DialogTitle>
            <DialogDescription>
              Re-queues as <strong>ready</strong> for the same agent. Optional notes go to <code className="text-xs">agent_log</code>.
              Leave empty to use Flume&apos;s default recovery hint (re-test, fix root cause, iterate).
            </DialogDescription>
          </DialogHeader>
          <Textarea
            placeholder="Optional: concrete steps or constraints for the agent…"
            value={unblockQueueNote}
            onChange={e => setUnblockQueueNote(e.target.value)}
            className="min-h-[100px] text-sm"
          />
          <DialogFooter className="gap-2 sm:gap-0">
            <button
              type="button"
              className="text-sm px-3 py-2 rounded-md border border-border text-muted-foreground hover:bg-muted/30"
              onClick={() => { setUnblockTarget(null); setUnblockQueueNote(''); }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="text-sm px-4 py-2 rounded-md bg-primary text-primary-foreground font-medium disabled:opacity-50"
              disabled={unblockQueueBusy}
              onClick={() => void submitQueueUnblock()}
            >
              {unblockQueueBusy ? 'Saving…' : 'Unblock & resume'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AgentThoughtDrawer taskId={thoughtTaskId} taskTitle={thoughtTaskTitle} taskStatus={thoughtTaskStatus} isOpen={drawerOpen} onOpenChange={setDrawerOpen} />

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">Work Queue</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {isLoading ? 'Loading…' : `Live pipeline — ${tasks.length} items`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button 
            onClick={() => setDialogAction('resume')}
            disabled={isHalting}
            className="flex items-center gap-2 px-4 py-2 bg-primary/10 text-primary hover:bg-primary/20 border border-primary/20 rounded-md text-sm font-medium transition-colors disabled:opacity-50"
          >
            {isHalting && dialogAction === 'resume' ? <Loader2 className="w-4 h-4 animate-spin" /> : <GitBranch className="w-4 h-4" />}
            Resume Swarms
          </button>
          <button 
            onClick={() => setDialogAction('halt')}
            disabled={isHalting}
            className="flex items-center gap-2 px-4 py-2 bg-destructive/10 text-destructive hover:bg-destructive/20 border border-destructive/20 rounded-md text-sm font-medium transition-colors disabled:opacity-50"
          >
            {isHalting && dialogAction === 'halt' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldAlert className="w-4 h-4" />}
            Halt All Swarms
          </button>
        </div>
      </motion.div>

      {isLoading && (
        <div className="flex items-center justify-center py-20 gap-3 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      )}

      {error && (
        <div className="flex items-center gap-3 p-4 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          Failed to connect to backend.
        </div>
      )}

      {!isLoading && !error && (
        <div className="flex gap-4 overflow-x-auto pb-4 relative z-10">
          {stages.map((stage, stageIdx) => {
            const items = tasks.filter(t => t.status === stage.id);
            return (
              <motion.div
                key={stage.id}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: stageIdx * 0.05 }}
                className="flex-shrink-0 w-[280px]"
              >
                <div className="flex items-center justify-between mb-3 px-1">
                  <div className="flex items-center gap-2">
                    <StatusBadge status={stage.id} />
                    <span className="text-[10px] text-muted-foreground">({items.length})</span>
                  </div>
                </div>

                <div className="space-y-2 min-h-[200px]">
                  {items.map((item, i) => {
                    const worker = workers.find(w => w.current_task_id === item.id);
                    const openThoughts = () => {
                      setThoughtTaskId(item.id);
                      setThoughtTaskTitle(item.title);
                      setThoughtTaskStatus(item.status);
                      setDrawerOpen(true);
                    };
                    const cardInner = (
                      <>
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${typeColors[item.work_item_type ?? item.item_type ?? 'task']}`}>
                            {(item.work_item_type ?? item.item_type ?? 'task').toUpperCase()}
                          </span>
                          <StatusBadge status={item.priority} />
                        </div>
                        <p className="text-xs text-foreground font-medium truncate">{item.title}</p>
                        <div className="flex items-start justify-between mt-2">
                          <span className="text-[10px] text-muted-foreground truncate">{item.repo}</span>
                          {worker && (
                            <div className="flex flex-col items-end shrink-0 ml-2">
                              <span className="text-[10px] font-medium text-primary truncate">{worker.name}</span>
                              <span className="text-[9px] text-muted-foreground truncate mt-0.5" title="Execution Node">{worker.execution_host || 'localhost'}</span>
                              <span className="text-[9px] text-muted-foreground truncate" title="Model">{worker.model || worker.preferred_model}</span>
                            </div>
                          )}
                        </div>
                        {item.branch && (
                          <div className="flex items-center gap-1 mt-1">
                            <GitBranch className="w-2.5 h-2.5 text-primary/50" />
                            <code className="text-[9px] text-muted-foreground truncate">{item.branch}</code>
                          </div>
                        )}
                        <div className="text-[9px] text-muted-foreground/50 mt-1">{timeAgo(item.last_update || item.updated_at)}</div>
                      </>
                    );
                    return (
                      <motion.div
                        key={item._id}
                        initial={{ opacity: 0, x: -8 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: stageIdx * 0.05 + i * 0.03 }}
                        whileHover={{ y: -2, transition: { duration: 0.15 } }}
                        className={`glass-card overflow-hidden flex flex-col ${stage.id === 'blocked' ? 'p-0' : 'p-3 hover-lift cursor-pointer'}`}
                        onClick={stage.id === 'blocked' ? undefined : openThoughts}
                      >
                        {stage.id === 'blocked' ? (
                          <>
                            <button
                              type="button"
                              className="p-3 text-left w-full hover:bg-muted/10 transition-colors"
                              onClick={openThoughts}
                            >
                              {cardInner}
                            </button>
                            <div className="flex gap-2 px-3 py-2 border-t border-border/30 bg-destructive/[0.03]">
                              <button
                                type="button"
                                className="text-[10px] px-2.5 py-1 rounded-md bg-warning/15 border border-warning/25 text-warning font-medium hover:bg-warning/25 transition-colors"
                                onClick={() => { setUnblockTarget(item); setUnblockQueueNote(''); }}
                              >
                                Unblock…
                              </button>
                              <span className="text-[9px] text-muted-foreground self-center flex-1">Add guidance, then resume the same role</span>
                            </div>
                          </>
                        ) : (
                          cardInner
                        )}
                      </motion.div>
                    );
                  })}
                  {items.length === 0 && (
                    <div className="glass-surface p-4 text-center text-xs text-muted-foreground">Empty</div>
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
