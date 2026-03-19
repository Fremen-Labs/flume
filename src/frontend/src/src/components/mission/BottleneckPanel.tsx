import { motion } from 'framer-motion';
import { AlertTriangle, ShieldAlert, AlertCircle, ArrowRight } from 'lucide-react';
import { bottleneckItems } from '@/data/mockData';
import type { BottleneckItem } from '@/types';

const severityConfig = {
  critical: { icon: ShieldAlert, color: 'text-destructive', bg: 'bg-destructive/10', border: 'border-destructive/20' },
  high: { icon: AlertTriangle, color: 'text-warning', bg: 'bg-warning/10', border: 'border-warning/20' },
  medium: { icon: AlertCircle, color: 'text-muted-foreground', bg: 'bg-muted/30', border: 'border-border' },
};

interface BottleneckPanelProps {
  compact?: boolean;
}

export function BottleneckPanel({ compact = false }: BottleneckPanelProps) {
  const items = compact ? bottleneckItems.slice(0, 3) : bottleneckItems;

  return (
    <div className="space-y-2">
      {items.map((item, i) => {
        const config = severityConfig[item.severity];
        const Icon = config.icon;
        return (
          <motion.div
            key={item.id}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.05 }}
            className={`glass-surface rounded-lg p-3 border ${config.border} hover:bg-white/[0.02] transition-colors`}
          >
            <div className="flex items-start gap-2.5">
              <div className={`p-1.5 rounded-md ${config.bg} flex-shrink-0`}>
                <Icon className={`w-3.5 h-3.5 ${config.color}`} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className={`text-[9px] font-semibold uppercase tracking-wider ${config.color}`}>{item.severity}</span>
                  <span className="text-[9px] text-muted-foreground">{item.category}</span>
                </div>
                <p className="text-xs text-foreground leading-relaxed">{item.description}</p>
                {!compact && (
                  <>
                    <div className="flex items-center gap-2 mt-1.5 text-[10px] text-muted-foreground">
                      <span>{item.affectedProject}</span>
                      <span>·</span>
                      <span>{item.affectedStage}</span>
                    </div>
                    <div className="flex items-center gap-1 mt-1.5 text-[10px] text-primary">
                      <ArrowRight className="w-2.5 h-2.5" />
                      <span>{item.suggestedAction}</span>
                    </div>
                  </>
                )}
              </div>
            </div>
          </motion.div>
        );
      })}
    </div>
  );
}