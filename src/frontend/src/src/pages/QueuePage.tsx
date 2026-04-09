import { motion } from 'framer-motion';
import { useState } from 'react';
import { Loader2, AlertCircle, GitBranch, ShieldAlert } from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';
import { AgentThoughtDrawer } from '@/components/AgentThoughtDrawer';
import { useToast } from '@/hooks/use-toast';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

const stages: { id: string; label: string }[] = [
  { id: 'inbox', label: 'Inbox' },
  { id: 'planned', label: 'Planned' },
  { id: 'ready', label: 'Ready' },
  { id: 'running', label: 'Running' },
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
  const { data: snapshot, isLoading, error, mutate } = useSnapshot();
  const { toast } = useToast();
  const [isHalting, setIsHalting] = useState(false);
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [adminToken, setAdminToken] = useState('');
  const [thoughtTaskId, setThoughtTaskId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];

  const handleConfirmHalt = async () => {
    try {
      setIsHalting(true);
      const res = await fetch('/api/tasks/stop-all', { 
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${adminToken}`
        }
      });
      if (res.ok) {
        mutate();
        setAdminToken('');
        toast({ title: "Swarms Halted", description: "All active tasks successfully halted." });
      } else {
        const errorBody = await res.json().catch(() => ({}));
        const description = errorBody.error 
          ? `Error: ${errorBody.error} (Request ID: ${errorBody.correlation_id || 'Unknown'})`
          : "An unknown error occurred resolving the native API.";
        toast({ title: "Halt Failed", description, variant: "destructive" });
      }
    } catch (e) {
      console.error(e);
      toast({ title: "System Exception", description: "Exception occurred triggering Kill Switch bounds.", variant: "destructive" });
    } finally {
      setIsHalting(false);
      setShowConfirmDialog(false);
    }
  };

  return (
    <div className="p-6 lg:p-8 max-w-[1800px] mx-auto space-y-6 relative">

      <Dialog open={showConfirmDialog} onOpenChange={setShowConfirmDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop All Swarms</DialogTitle>
            <DialogDescription className="space-y-3 pt-2">
              <p>Are you sure you want to stop all active tasks? This will immediately terminate running processes and block all queued tasks from starting.</p>
              <Input 
                type="password" 
                placeholder="Flume Admin Token" 
                value={adminToken} 
                onChange={(e) => setAdminToken(e.target.value)} 
                className="w-full"
              />
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-0 mt-4">
            <button
              onClick={() => setShowConfirmDialog(false)}
              className="px-4 py-2 border rounded-md text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={handleConfirmHalt}
              disabled={isHalting}
              className="px-4 py-2 bg-destructive text-destructive-foreground hover:bg-destructive/90 rounded-md text-sm flex items-center justify-center gap-2"
            >
              {isHalting && <Loader2 className="w-4 h-4 animate-spin" />}
              {isHalting ? 'Terminating...' : 'Force Kill Processes'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AgentThoughtDrawer taskId={thoughtTaskId} isOpen={drawerOpen} onOpenChange={setDrawerOpen} />

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">Work Queue</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {isLoading ? 'Loading…' : `Live pipeline — ${tasks.length} items`}
          </p>
        </div>
        <button 
          onClick={() => setShowConfirmDialog(true)}
          disabled={isHalting}
          className="flex items-center gap-2 px-4 py-2 bg-destructive/10 text-destructive hover:bg-destructive/20 border border-destructive/20 rounded-md text-sm font-medium transition-colors disabled:opacity-50"
        >
          {isHalting ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldAlert className="w-4 h-4" />}
          {isHalting ? 'Halting LLM Generation...' : 'Halt All Swarms'}
        </button>
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
                    return (
                      <motion.div
                        key={item._id}
                        initial={{ opacity: 0, x: -8 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{ delay: stageIdx * 0.05 + i * 0.03 }}
                        whileHover={{ y: -2, transition: { duration: 0.15 } }}
                        onClick={() => { setThoughtTaskId(item.id); setDrawerOpen(true); }}
                        className="glass-card p-3 hover-lift cursor-pointer"
                      >
                        <div className="flex items-center gap-2 mb-1.5">
                          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${typeColors[item.work_item_type ?? item.item_type ?? 'task']}`}>
                            {(item.work_item_type ?? item.item_type ?? 'task').toUpperCase()}
                          </span>
                          <StatusBadge status={item.priority} />
                        </div>
                        <p className="text-xs text-foreground font-medium truncate">{item.title}</p>
                        <div className="flex items-center justify-between mt-2">
                          <span className="text-[10px] text-muted-foreground truncate">{item.repo}</span>
                          {worker && (
                            <span className="text-[10px] text-primary truncate ml-2">{worker.name}</span>
                          )}
                        </div>
                        {item.branch && (
                          <div className="flex items-center gap-1 mt-1">
                            <GitBranch className="w-2.5 h-2.5 text-primary/50" />
                            <code className="text-[9px] text-muted-foreground truncate">{item.branch}</code>
                          </div>
                        )}
                        <div className="text-[9px] text-muted-foreground/50 mt-1">{timeAgo(item.last_update || item.updated_at)}</div>
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
