import { ReactNode } from 'react';
import { motion } from 'framer-motion';
import { LucideIcon } from 'lucide-react';

interface GlassMetricCardProps {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: LucideIcon;
  trend?: { value: number; label: string; suffix?: string };
  glow?: boolean;
  className?: string;
  children?: ReactNode;

  // Grok uplift cross-cutting: resilient states for all Analytics metric cards
  loading?: boolean;
  error?: string | null;
  partial?: boolean;
  helpText?: string; // tooltip explaining what this metric actually measures post-uplift
  secondary?: { value?: string | number; label?: string };
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
  // Grok uplift (AST Savings + cross-cutting): helpText + resilience states now wired.
  // helpText renders as inline info affordance (native title tooltip for zero-dep trustworthiness).
  // partial/error/loading provide visual cues so silent-zero / missing-Elastro cases are not mysterious.
  loading,
  error,
  partial,
  helpText,
  secondary,
}: GlassMetricCardProps) {
  // Minimal resilience visuals (no heavy conditional DOM bloat)
  const cardClasses = [
    glow ? 'glass-card-glow' : 'glass-card',
    'p-5 hover-lift',
    partial ? 'opacity-75' : '',
    error ? 'border-destructive/40' : '',
    className,
  ].filter(Boolean).join(' ');

  const valueClasses = [
    'text-3xl font-bold tracking-tight relative z-10',
    error ? 'text-destructive' : 'text-foreground',
    partial ? 'text-muted-foreground' : '',
  ].filter(Boolean).join(' ');

  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      whileHover={{ y: -3, transition: { duration: 0.2 } }}
      className={cardClasses}
    >
      <div className="flex items-start justify-between mb-3 relative z-10">
        <span className="text-xs font-medium tracking-wider uppercase text-muted-foreground flex items-center gap-1">
          {title}
          {helpText && (
            <span
              className="ml-0.5 cursor-help text-[10px] opacity-60 hover:opacity-100 select-none"
              title={helpText}
              aria-label="More info about this metric"
            >
              ⓘ
            </span>
          )}
        </span>
        {Icon && (
          <div className="p-2 rounded-lg bg-primary/10">
            <Icon className="w-4 h-4 text-primary" />
          </div>
        )}
      </div>
      <div className={valueClasses}>
        {loading ? '…' : value}
      </div>
      {subtitle && <p className="text-sm text-muted-foreground mt-1 relative z-10">{subtitle}</p>}
      {helpText && !subtitle && (
        // Fallback: also surface helpText as very subtle subtitle when no explicit subtitle provided
        // (keeps AST Savings explanation visible even if caller uses helpText only)
        <p className="text-[10px] text-muted-foreground/70 mt-1 leading-snug line-clamp-2 relative z-10" title={helpText}>
          {helpText.length > 120 ? helpText.slice(0, 117) + '…' : helpText}
        </p>
      )}
      {error && <p className="text-[10px] text-destructive mt-1">Error: {error}</p>}
      {partial && !error && (
        <p className="text-[10px] text-amber-500/80 mt-0.5">Partial / no Elastro data yet</p>
      )}
      {trend && (
        <div className="flex items-center gap-1 mt-2 relative z-10">
          <span className={`text-xs font-medium ${trend.value >= 0 ? 'text-success' : 'text-destructive'}`}>
            {trend.value >= 0 ? '+' : ''}{trend.value}{trend.suffix ?? '%'}
          </span>
          <span className="text-xs text-muted-foreground">{trend.label}</span>
        </div>
      )}
      {secondary && (secondary.value != null || secondary.label) && (
        <div className="text-[10px] text-muted-foreground mt-0.5">{secondary.label}: {secondary.value}</div>
      )}
      {children && <div className="relative z-10">{children}</div>}
    </motion.div>
  );
}