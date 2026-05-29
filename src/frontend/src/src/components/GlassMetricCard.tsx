import { ReactNode } from 'react';
import { motion } from 'framer-motion';
import { LucideIcon, Info, AlertTriangle, AlertCircle, Loader2 } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

interface GlassMetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: LucideIcon;
  trend?: { value: number; label: string; suffix?: string };
  glow?: boolean;
  className?: string;
  children?: ReactNode;

  // Grok uplift cross-cutting: resilient states + help for all Analytics metric cards
  // (previously declared but never implemented/used — now fully activated)
  loading?: boolean;
  error?: string | null;
  partial?: boolean;
  helpText?: string; // rich tooltip + native title explaining the metric (per review: VRAM/AST/NodeMesh etc.)
  secondary?: { value?: string | number; label?: string }; // non-delta supplementary label/value row
}

export function GlassMetricCard({
  title,
  value,
  subtitle,
  icon: Icon,
  trend,
  glow,
  className = '',
  children,
  loading = false,
  error = null,
  partial = false,
  helpText,
  secondary,
}: GlassMetricCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      whileHover={{ y: -3, transition: { duration: 0.2 } }}
      className={`${glow ? 'glass-card-glow' : 'glass-card'} p-5 hover-lift ${className}`}
    >
      <div className="flex items-start justify-between mb-3 relative z-10">
        <span
          className="text-xs font-medium tracking-wider uppercase text-muted-foreground flex items-center gap-1.5"
          title={helpText}
        >
          {title}
          {helpText && (
            <Tooltip delayDuration={120}>
              <TooltipTrigger asChild>
                <Info
                  className="w-3 h-3 text-muted-foreground/60 hover:text-muted-foreground transition-colors cursor-help"
                  aria-label={`Info about ${title}`}
                />
              </TooltipTrigger>
              <TooltipContent
                side="top"
                align="start"
                className="max-w-[260px] text-xs leading-relaxed bg-popover/95 backdrop-blur border-border/60 p-2.5 shadow-xl"
              >
                {helpText}
              </TooltipContent>
            </Tooltip>
          )}
        </span>
        {Icon && (
          <div className="p-2 rounded-lg bg-primary/10">
            <Icon className="w-4 h-4 text-primary" />
          </div>
        )}
      </div>

      {/* Value area with loading / error resilience states */}
      {loading ? (
        <Skeleton className="h-8 w-24 mt-0.5 relative z-10" />
      ) : error ? (
        <div className="text-3xl font-bold tracking-tight text-destructive/60 relative z-10">—</div>
      ) : (
        <div className="text-3xl font-bold tracking-tight text-foreground relative z-10">{value}</div>
      )}

      {!loading && !error && subtitle && (
        <p className="text-sm text-muted-foreground mt-1 relative z-10">{subtitle}</p>
      )}

      {!loading && !error && trend && (
        <div className="flex items-center gap-1 mt-2 relative z-10">
          <span className={`text-xs font-medium ${trend.value >= 0 ? 'text-success' : 'text-destructive'}`}>
            {trend.value >= 0 ? '+' : ''}{trend.value}{trend.suffix ?? '%'}
          </span>
          <span className="text-xs text-muted-foreground">{trend.label}</span>
        </div>
      )}

      {/* Cross-cutting resilience indicators (spinner, error banner with icon, partial warning) */}
      {loading && !error && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground relative z-10">
          <Loader2 className="w-3 h-3 animate-spin" />
          <span>Loading…</span>
        </div>
      )}

      {error && (
        <div className="mt-2 flex items-start gap-1.5 text-xs text-destructive bg-destructive/10 border border-destructive/20 px-2.5 py-1.5 rounded-md relative z-10">
          <AlertTriangle className="w-3.5 h-3.5 mt-px flex-shrink-0" />
          <span className="leading-snug break-words">{error}</span>
        </div>
      )}

      {!loading && !error && partial && (
        <div className="mt-1.5 inline-flex items-center gap-1 text-[10px] font-medium text-amber-600 dark:text-amber-500 bg-amber-500/10 px-1.5 py-0.5 rounded relative z-10">
          <AlertCircle className="w-3 h-3" />
          PARTIAL DATA
        </div>
      )}

      {/* Secondary non-delta row for supplementary labels/values (consistent with uplift) */}
      {!loading && !error && secondary && (secondary.label || secondary.value != null) && (
        <div className="mt-2.5 pt-2 border-t border-white/10 text-[11px] text-muted-foreground flex items-baseline gap-1.5 relative z-10">
          {secondary.label && <span className="uppercase tracking-[0.5px] opacity-70">{secondary.label}</span>}
          {secondary.value != null && <span className="font-mono text-foreground/85 tracking-tight">{secondary.value}</span>}
        </div>
      )}

      {children && <div className="relative z-10">{children}</div>}
    </motion.div>
  );
}