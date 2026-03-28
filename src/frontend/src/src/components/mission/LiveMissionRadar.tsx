import { motion, AnimatePresence } from 'framer-motion';
import { Radar, Activity, CheckCircle2, Clock, Server, Monitor, TerminalSquare } from 'lucide-react';
import { StatusBadge } from '@/components/StatusBadge';
import { Line, LineChart, ResponsiveContainer } from 'recharts';
import { useSystemState } from '../../hooks/useSystemState';

export function LiveMissionRadar() {
  const { data, history, logs, isLoading } = useSystemState();

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
        <div className="glass-surface p-4 rounded-xl relative overflow-hidden border border-white/[0.02]">
          <div className="relative z-10 flex flex-col items-center">
            <div className="text-2xl font-bold text-foreground">{data.workers.length}</div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Total Nodes</div>
          </div>
        </div>
        <div className="glass-surface p-4 rounded-xl border border-primary/30 relative overflow-hidden group">
          <div className="absolute inset-x-0 bottom-0 h-16 opacity-20 group-hover:opacity-40 transition-opacity">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history}>
                <Line type="monotone" dataKey="active" stroke="hsl(var(--primary))" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="relative z-10 flex flex-col items-center">
            <div className="text-2xl font-bold text-primary">{activeWorkers.length}</div>
            <div className="text-[10px] text-primary/70 uppercase tracking-wider mt-1">Active Streams</div>
          </div>
        </div>
        <div className="glass-surface p-4 rounded-xl border border-white/[0.05] relative overflow-hidden group">
          <div className="absolute inset-x-0 bottom-0 h-16 opacity-10 group-hover:opacity-20 transition-opacity">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history}>
                <Line type="monotone" dataKey="idle" stroke="hsl(var(--muted-foreground))" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="relative z-10 flex flex-col items-center">
            <div className="text-2xl font-bold text-foreground">{idleWorkers.length}</div>
            <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Standby</div>
          </div>
        </div>
        <div className="glass-surface p-4 rounded-xl relative overflow-hidden border border-success/20 bg-success/5 flex flex-col items-center justify-center">
          <div className="text-2xl font-bold text-success flex items-center justify-center gap-2">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-success opacity-75"></span>
              <span className="relative inline-flex rounded-full h-3 w-3 bg-success"></span>
            </span>
            ON
          </div>
          <div className="text-[10px] text-success/70 uppercase tracking-wider mt-1 font-semibold">Grid Status</div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-5">
        <div className="lg:col-span-3">
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

        {/* Live Telemetry Feed (Sidebar) */}
        <div className="lg:col-span-1 border border-white/[0.05] rounded-xl flex flex-col overflow-hidden bg-black/60 shadow-inner">
          <div className="p-3 border-b border-white/[0.05] bg-white/[0.01] flex items-center gap-2">
            <TerminalSquare className="w-3.5 h-3.5 text-muted-foreground" />
            <h3 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">Live Telemetry Feed</h3>
            <div className="ml-auto w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2 h-[400px] lg:h-auto custom-scrollbar">
            {logs.length === 0 ? (
              <div className="text-[10px] text-muted-foreground/30 text-center py-4 font-mono">Awaiting stream...</div>
            ) : (
              logs.map((log) => (
                <div key={log.id} className="text-[10px] font-mono leading-relaxed">
                  <span className="text-muted-foreground/50">[{log.time}]</span> 
                  <span className={log.level === 'INFO' ? 'text-primary/90' : 'text-success/90'}> {log.msg}</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
