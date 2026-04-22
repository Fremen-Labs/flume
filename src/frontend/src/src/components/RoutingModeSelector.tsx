import { motion } from 'framer-motion';
import { Globe, Zap, Monitor } from 'lucide-react';
import type { RoutingMode } from '@/types';

const MODES: { id: RoutingMode; label: string; description: string; icon: typeof Globe }[] = [
  {
    id: 'frontier_only',
    label: 'Frontier Only',
    description: 'Route all LLM requests to cloud providers. Best quality, API costs apply.',
    icon: Globe,
  },
  {
    id: 'hybrid',
    label: 'Hybrid',
    description: 'Blend local and cloud intelligence. Balance cost vs quality with tunable controls.',
    icon: Zap,
  },
  {
    id: 'local_only',
    label: 'Local Only',
    description: 'Use only registered Ollama nodes. Zero cloud costs, VRAM-bounded throughput.',
    icon: Monitor,
  },
];

interface Props {
  mode: RoutingMode;
  onChange: (mode: RoutingMode) => void;
  disabled?: boolean;
}

export function RoutingModeSelector({ mode, onChange, disabled }: Props) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3" id="routing-mode-selector">
      {MODES.map(({ id, label, description, icon: Icon }) => {
        const active = mode === id;
        return (
          <motion.button
            key={id}
            id={`routing-mode-${id}`}
            disabled={disabled}
            onClick={() => onChange(id)}
            whileHover={{ scale: 1.01 }}
            whileTap={{ scale: 0.99 }}
            className={`relative p-4 rounded-xl text-left transition-all duration-300 border ${
              active
                ? 'bg-white/[0.07] border-indigo-500/60 shadow-[0_0_20px_rgba(99,102,241,0.12)]'
                : 'bg-white/[0.02] border-border/20 hover:bg-white/[0.04] hover:border-border/40'
            } ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
          >
            {/* Glow ring for active state */}
            {active && (
              <motion.div
                layoutId="routing-mode-glow"
                className="absolute inset-0 rounded-xl border-2 border-indigo-500/30"
                style={{ boxShadow: '0 0 24px rgba(99,102,241,0.15)' }}
                transition={{ type: 'spring', stiffness: 300, damping: 30 }}
              />
            )}

            <div className="relative flex items-start gap-3">
              <div className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${
                active
                  ? 'bg-indigo-500/20 text-indigo-400'
                  : 'bg-white/5 text-muted-foreground'
              }`}>
                <Icon className="w-4.5 h-4.5" />
              </div>
              <div className="min-w-0">
                <p className={`text-sm font-semibold ${active ? 'text-foreground' : 'text-muted-foreground'}`}>
                  {label}
                </p>
                <p className="text-[11px] text-muted-foreground/70 mt-0.5 leading-relaxed">
                  {description}
                </p>
              </div>
            </div>

            {/* Active indicator dot */}
            {active && (
              <motion.div
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                className="absolute top-3 right-3 w-2 h-2 rounded-full bg-indigo-400"
                style={{ boxShadow: '0 0 8px rgba(99,102,241,0.5)' }}
              />
            )}
          </motion.button>
        );
      })}
    </div>
  );
}
