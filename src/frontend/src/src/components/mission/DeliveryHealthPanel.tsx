import { motion } from 'framer-motion';
import { systemMetrics } from '@/data/mockData';
import { ProgressRing } from '@/components/ProgressRing';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface MetricGaugeProps {
  label: string;
  value: number;
  unit?: string;
  maxValue?: number;
  trend?: number;
  color?: string;
}

function MetricGauge({ label, value, unit = '', maxValue = 100, trend, color }: MetricGaugeProps) {
  const percentage = Math.min((value / maxValue) * 100, 100);
  const TrendIcon = trend && trend > 0 ? TrendingUp : trend && trend < 0 ? TrendingDown : Minus;
  const trendColor = trend && trend > 0 ? 'text-success' : trend && trend < 0 ? 'text-destructive' : 'text-muted-foreground';

  return (
    <div className="glass-surface rounded-lg p-3 text-center">
      <ProgressRing value={percentage} size={44} strokeWidth={3} />
      <div className="mt-2">
        <div className="text-sm font-bold text-foreground">{value}{unit}</div>
        <div className="text-[10px] text-muted-foreground">{label}</div>
        {trend !== undefined && (
          <div className={`flex items-center justify-center gap-0.5 mt-0.5 ${trendColor}`}>
            <TrendIcon className="w-2.5 h-2.5" />
            <span className="text-[9px] font-medium">{trend > 0 ? '+' : ''}{trend}%</span>
          </div>
        )}
      </div>
    </div>
  );
}

interface DeliveryHealthPanelProps {
  compact?: boolean;
}

export function DeliveryHealthPanel({ compact = false }: DeliveryHealthPanelProps) {
  const m = systemMetrics;

  if (compact) {
    return (
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <ProgressRing value={m.healthScore} size={36} strokeWidth={3} />
          <div>
            <div className="text-sm font-bold text-foreground">{m.healthScore}</div>
            <div className="text-[10px] text-muted-foreground">Health</div>
          </div>
        </div>
        <div className="h-6 w-px bg-white/[0.06]" />
        <div className="flex gap-3 text-center">
          <div><div className="text-xs font-bold text-foreground">{m.throughputScore}</div><div className="text-[9px] text-muted-foreground">Throughput</div></div>
          <div><div className="text-xs font-bold text-foreground">{m.blockedRatio}%</div><div className="text-[9px] text-muted-foreground">Blocked</div></div>
          <div><div className="text-xs font-bold text-success">{m.testingPassRate}%</div><div className="text-[9px] text-muted-foreground">Pass Rate</div></div>
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Main health score */}
      <div className="flex items-center gap-4 mb-4">
        <motion.div initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 100 }}>
          <ProgressRing value={m.healthScore} size={72} strokeWidth={4} />
        </motion.div>
        <div>
          <div className="text-2xl font-bold text-foreground">{m.healthScore}<span className="text-sm text-muted-foreground">/100</span></div>
          <div className="text-xs text-muted-foreground">System Health Index</div>
          <div className="flex items-center gap-1 mt-0.5 text-success">
            <TrendingUp className="w-3 h-3" />
            <span className="text-[10px] font-medium">+{m.velocityTrend}% this week</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <MetricGauge label="Throughput" value={m.throughputScore} trend={8} />
        <MetricGauge label="Blocked %" value={m.blockedRatio} maxValue={20} unit="%" trend={-15} />
        <MetricGauge label="Velocity" value={m.completionVelocity} maxValue={30} unit="/d" trend={m.velocityTrend} />
        <MetricGauge label="Review Time" value={m.reviewTurnaround} maxValue={8} unit="h" trend={-12} />
        <MetricGauge label="Test Pass" value={m.testingPassRate} unit="%" trend={3} />
        <MetricGauge label="Response" value={m.agentResponseTime} maxValue={5} unit="s" trend={-8} />
      </div>
    </div>
  );
}