import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Radar, Activity, CheckCircle2, Clock, Server, Monitor } from 'lucide-react';
import { StatusBadge } from '@/components/StatusBadge';

interface WorkerState {
  name: string;
  role: string;
  model: string;
  execution_host: string;
  llm_provider: string;
  status: string;
  current_task_title?: string;
  heartbeat_at: string;
}

interface SystemState {
  updated_at: string;
  workers: WorkerState[];
}

export function LiveMissionRadar() {
  const [data, setData] = useState<SystemState | null>(null);

  useEffect(() => {
    const fetchState = async () => {
      try {
        const res = await fetch('/api/system-state');
        if (res.ok) {
          const json = await res.json();
          setData(json);
        }
      } catch (e) {
        console.error('Failed to fetch system state', e);
      }
    };
    
    fetchState();
    const interval = setInterval(fetchState, 2000);
    return () => clearInterval(interval);
  }, []);

  if (!data) {
    return (
      <div className="flex items-center justify-center p-12 text-muted-foreground">
        <Activity className="w-5 h-5 mr-3 animate-pulse" />
        Establishing Telemetry Link...
      </div>
    );
  }

  const activeWorkers = data.workers.filter(w => w.status === 'claimed' || w.status === 'active');
  const idleWorkers = data.workers.filter(w => w.status === 'idle');

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="glass-surface p-4 rounded-xl text-center hover:bg-white/[0.02] transition-colors border border-white/[0.02]">
          <div className="text-2xl font-bold text-foreground">{data.workers.length}</div>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Total Nodes</div>
        </div>
        <div className="glass-surface p-4 rounded-xl text-center border border-primary/30 relative overflow-hidden group">
          <div className="absolute inset-0 bg-primary/5 opacity-0 group-hover:opacity-100 transition-opacity" />
          <div className="text-2xl font-bold text-primary relative z-10">{activeWorkers.length}</div>
          <div className="text-[10px] text-primary/70 uppercase tracking-wider mt-1 relative z-10">Active Streams</div>
        </div>
        <div className="glass-surface p-4 rounded-xl text-center hover:bg-white/[0.02] transition-colors border border-white/[0.02]">
          <div className="text-2xl font-bold text-foreground">{idleWorkers.length}</div>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Standby</div>
        </div>
        <div className="glass-surface p-4 rounded-xl text-center hover:bg-white/[0.02] transition-colors border border-white/[0.02]">
          <div className="text-2xl font-bold text-success flex items-center justify-center gap-1.5">
            <CheckCircle2 className="w-4 h-4" /> ON
          </div>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Grid Status</div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        <AnimatePresence>
          {data.workers.map((worker, i) => {
            const isActive = worker.status === 'claimed' || worker.status === 'active';
            return (
              <motion.div
                key={worker.name}
                initial={{ opacity: 0, y: 15 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                transition={{ delay: i * 0.05, type: 'spring', stiffness: 100 }}
                className={`glass-panel p-5 rounded-xl border relative overflow-hidden ${isActive ? 'border-primary/40' : 'border-white/[0.05]'}`}
              >
                {isActive && (
                  <div className="absolute top-0 right-0 w-32 h-32 bg-primary/10 rounded-full blur-3xl -mr-10 -mt-10 pointer-events-none" />
                )}
                
                <div className="flex justify-between items-start mb-4 relative z-10">
                  <div>
                    <h3 className="text-sm font-bold text-foreground flex items-center gap-2">
                      {worker.name}
                      {isActive && <Radar className="w-3.5 h-3.5 text-primary animate-spin-slow" />}
                    </h3>
                    <div className="text-[10px] text-muted-foreground mt-1 uppercase tracking-wider font-semibold">
                      {worker.role} <span className="text-white/20 px-1">·</span> {worker.model}
                    </div>
                  </div>
                  <StatusBadge status={isActive ? 'active' : 'idle'} pulse={isActive} />
                </div>

                <div className="space-y-3 relative z-10">
                  <div className="bg-black/40 rounded-lg p-3 border border-white/[0.04]">
                    <div className="text-[9px] font-semibold text-muted-foreground uppercase tracking-wider mb-1.5 flex items-center gap-1.5">
                      <Clock className="w-3 h-3 text-primary/70" /> Current Execution
                    </div>
                    <div className={`text-xs ${isActive ? 'text-primary/90' : 'text-muted-foreground/40'}`}>
                      {worker.current_task_title || "Awaiting Instructions..."}
                    </div>
                  </div>

                  <div className="flex items-center justify-between text-[10px] pt-1">
                    <span className="text-muted-foreground flex items-center gap-1.5">
                      <Server className="w-3.5 h-3.5" /> <span className="capitalize">{worker.llm_provider}</span>
                    </span>
                    <span className="text-muted-foreground flex items-center gap-1.5 truncate max-w-[120px]">
                      <Monitor className="w-3.5 h-3.5" /> {worker.execution_host.split('.')[0]}
                    </span>
                  </div>
                </div>
              </motion.div>
            );
          })}
        </AnimatePresence>
      </div>
    </div>
  );
}
