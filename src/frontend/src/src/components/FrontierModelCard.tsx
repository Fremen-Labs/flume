import { motion } from 'framer-motion';
import { Trash2, ShieldAlert, ShieldCheck, ShieldX } from 'lucide-react';
import type { FrontierModelWeight } from '@/types';

// Provider color mapping for visual differentiation
const PROVIDER_COLORS: Record<string, { gradient: string; badge: string; icon: string }> = {
  openai:    { gradient: 'from-emerald-500/20 to-emerald-600/5', badge: 'bg-emerald-500/15 text-emerald-400', icon: '🟢' },
  anthropic: { gradient: 'from-amber-500/20 to-amber-600/5',    badge: 'bg-amber-500/15 text-amber-400',     icon: '🟠' },
  gemini:    { gradient: 'from-blue-500/20 to-blue-600/5',       badge: 'bg-blue-500/15 text-blue-400',       icon: '🔵' },
  xai:       { gradient: 'from-violet-500/20 to-violet-600/5',   badge: 'bg-violet-500/15 text-violet-400',   icon: '🟣' },
};

interface Props {
  model: FrontierModelWeight;
  onWeightChange: (weight: number) => void;
  onBudgetChange: (budget: number) => void;
  onRemove: () => void;
}

export function FrontierModelCard({ model, onWeightChange, onBudgetChange, onRemove }: Props) {
  const colors = PROVIDER_COLORS[model.provider] ?? PROVIDER_COLORS.openai;
  const utilization = model.budget_usd > 0 ? (model.spent_usd / model.budget_usd) * 100 : 0;

  const circuitColor = model.circuit_open
    ? 'text-red-400'
    : utilization >= 90
      ? 'text-amber-400'
      : 'text-emerald-400';

  const CircuitIcon = model.circuit_open
    ? ShieldX
    : utilization >= 90
      ? ShieldAlert
      : ShieldCheck;

  const barColor = model.circuit_open
    ? 'bg-red-500'
    : utilization >= 90
      ? 'bg-amber-500'
      : utilization >= 50
        ? 'bg-yellow-500'
        : 'bg-emerald-500';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className={`relative rounded-xl border border-border/20 bg-gradient-to-br ${colors.gradient} p-4 space-y-3`}
      id={`frontier-model-${model.model}`}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm">{colors.icon}</span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-foreground truncate">{model.model}</p>
            <p className="text-[10px] text-muted-foreground/70">{model.provider}</p>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <CircuitIcon className={`w-4 h-4 ${circuitColor}`} />
          <button
            onClick={onRemove}
            className="p-1 rounded-md text-muted-foreground/50 hover:text-red-400 hover:bg-red-400/10 transition-colors"
            title="Remove model"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Weight slider */}
      <div className="space-y-1">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-muted-foreground">Weight</span>
          <span className="font-mono text-foreground">{Math.round(model.weight * 100)}%</span>
        </div>
        <input
          type="range"
          min={0}
          max={100}
          value={Math.round(model.weight * 100)}
          onChange={(e) => onWeightChange(Number(e.target.value) / 100)}
          className="w-full h-1.5 rounded-full appearance-none bg-white/10 accent-indigo-500 cursor-pointer"
        />
      </div>

      {/* Budget & Spend */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-muted-foreground">Budget</span>
          <div className="flex items-center gap-1">
            <span className="text-muted-foreground/60">$</span>
            <input
              type="number"
              min={0}
              step={5}
              value={model.budget_usd}
              onChange={(e) => onBudgetChange(Math.max(0, Number(e.target.value)))}
              className="w-16 bg-transparent border-b border-border/30 text-right text-foreground font-mono text-[11px] focus:outline-none focus:border-indigo-500/50 py-0.5"
            />
          </div>
        </div>

        {/* Spend progress bar */}
        <div className="h-1.5 rounded-full bg-white/8 overflow-hidden">
          <motion.div
            className={`h-full rounded-full ${barColor}`}
            initial={{ width: 0 }}
            animate={{ width: `${Math.min(100, utilization)}%` }}
            transition={{ duration: 0.6, ease: 'easeOut' }}
          />
        </div>

        <div className="flex items-center justify-between text-[10px] text-muted-foreground/70">
          <span className="font-mono">
            ${model.spent_usd.toFixed(2)} / ${model.budget_usd.toFixed(2)}
          </span>
          <span className={`font-medium ${circuitColor}`}>
            {model.circuit_open ? 'CIRCUIT OPEN' : `${utilization.toFixed(1)}%`}
          </span>
        </div>
      </div>
    </motion.div>
  );
}
