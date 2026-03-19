import { useState } from 'react';
import { motion } from 'framer-motion';
import {
  Radar, Activity, Network, Flame, Heart, ArrowRightLeft, BarChart3,
  Filter
} from 'lucide-react';
import { AgentRadar } from '@/components/mission/AgentRadar';
import { HandoffStream } from '@/components/mission/HandoffStream';
import { DeliveryHealthPanel } from '@/components/mission/DeliveryHealthPanel';
import { BottleneckPanel } from '@/components/mission/BottleneckPanel';
import { ThroughputHeatmap } from '@/components/mission/ThroughputHeatmap';
import { SwarmView } from '@/components/mission/SwarmView';

function SectionHeader({ title, icon: Icon }: { title: string; icon: React.ElementType }) {
  return (
    <div className="flex items-center gap-2.5 mb-4">
      <div className="p-1.5 rounded-md bg-primary/10">
        <Icon className="w-3.5 h-3.5 text-primary" />
      </div>
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <div className="flex-1 border-t border-dashed border-white/[0.06]" />
    </div>
  );
}

export default function MissionControlPage() {
  return (
    <div className="p-5 lg:p-6 max-w-[1600px] mx-auto space-y-6 relative">

      {/* Header */}
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

      {/* Row 1: Health + Bottlenecks */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-5 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.05 }}
          className="lg:col-span-2 glass-panel p-5"
        >
          <SectionHeader title="Delivery Health" icon={Heart} />
          <div className="relative z-10">
            <DeliveryHealthPanel />
          </div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="lg:col-span-3 glass-panel p-5"
        >
          <SectionHeader title="Bottleneck Intelligence" icon={Flame} />
          <div className="relative z-10">
            <BottleneckPanel />
          </div>
        </motion.div>
      </div>

      {/* Row 2: Agent Radar + Swarm */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15 }}
          className="lg:col-span-2 glass-panel p-5"
        >
          <SectionHeader title="Agent Activity Radar" icon={Radar} />
          <div className="relative z-10">
            <AgentRadar />
          </div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
          className="glass-panel p-5"
        >
          <SectionHeader title="AI Swarm Distribution" icon={Network} />
          <div className="relative z-10">
            <SwarmView />
          </div>
        </motion.div>
      </div>

      {/* Row 3: Heatmap + Handoff Stream */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-5 relative z-10">
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.25 }}
          className="lg:col-span-3 glass-panel p-5"
        >
          <SectionHeader title="Throughput Heatmap" icon={BarChart3} />
          <ThroughputHeatmap />
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3 }}
          className="lg:col-span-2 glass-panel p-5"
        >
          <SectionHeader title="Handoff Stream" icon={ArrowRightLeft} />
          <div className="relative z-10">
            <HandoffStream />
          </div>
        </motion.div>
      </div>
    </div>
  );
}