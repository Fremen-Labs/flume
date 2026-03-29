import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Radar, PowerOff, ShieldAlert, Loader2 } from 'lucide-react';
import { LiveMissionRadar } from '@/components/mission/LiveMissionRadar';
import { toast } from 'sonner';

export default function MissionControlPage() {
  const [haltingState, setHaltingState] = useState<'idle' | 'confirm' | 'halting'>('idle');

  const triggerKillSwitch = async () => {
    setHaltingState('halting');
    try {
      const res = await fetch('/api/tasks/stop-all', {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer local-admin-1234'
        }
      });
      if (res.ok) {
        toast.success("Grid Halted Successfully", { description: "All autonomous workers and background threads have been forcefully terminated." });
      } else {
        toast.error("Kill Switch Failed", { description: "The Orchestrator refused the shutdown command." });
      }
    } catch(e) {
      toast.error("Grid Timeout", { description: "The API endpoint was unreachable natively." });
    }
    setHaltingState('idle');
  };

  return (
    <div className="p-5 lg:p-6 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center justify-between relative z-10"
      >
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-primary/15 flex items-center justify-center breathing">
            <Radar className="w-5 h-5 text-primary icon-glow-active" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-foreground">Mission Control</h1>
            <p className="text-xs text-muted-foreground">Live autonomous delivery operations</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          
          <AnimatePresence mode="wait">
            {haltingState === 'idle' && (
              <motion.button
                key="idle"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                onClick={() => setHaltingState('confirm')}
                className="glass-card border-destructive/30 hover:bg-destructive/10 px-4 py-1.5 flex items-center gap-2 text-xs text-destructive hover:text-red-400 transition-colors font-medium rounded-md group"
              >
                <PowerOff className="w-3.5 h-3.5 group-hover:scale-110 transition-transform" />
                Halt Grid
              </motion.button>
            )}
            
            {haltingState === 'confirm' && (
              <motion.div
                key="confirm"
                initial={{ opacity: 0, scale: 0.95, x: 20 }}
                animate={{ opacity: 1, scale: 1, x: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="flex items-center gap-2"
              >
                <button
                  onClick={triggerKillSwitch}
                  className="bg-destructive hover:bg-red-600 text-white px-4 py-1.5 flex items-center gap-2 text-xs font-bold rounded-md shadow-[0_0_15px_rgba(239,68,68,0.4)] transition-colors"
                >
                  <ShieldAlert className="w-3.5 h-3.5" /> Confirm Terminate?
                </button>
                <button
                  onClick={() => setHaltingState('idle')}
                  className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
              </motion.div>
            )}

            {haltingState === 'halting' && (
              <motion.div
                key="halting"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="glass-card border-destructive/50 bg-destructive/10 px-4 py-1.5 flex items-center gap-2 text-xs text-destructive font-medium rounded-md"
              >
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Terminating Threads...
              </motion.div>
            )}
          </AnimatePresence>

          <div className="w-px h-5 bg-white/10 mx-2" />

          <div className="glass-card px-3 py-1.5 flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-success opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-success" />
            </span>
            <span className="text-foreground font-medium">System Online</span>
          </div>
        </div>
      </motion.div>

      <div className="relative z-10">
        <LiveMissionRadar />
      </div>
    </div>
  );
}