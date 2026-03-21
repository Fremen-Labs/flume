import { motion } from 'framer-motion';
import { Radar } from 'lucide-react';
import { LiveMissionRadar } from '@/components/mission/LiveMissionRadar';

export default function MissionControlPage() {
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