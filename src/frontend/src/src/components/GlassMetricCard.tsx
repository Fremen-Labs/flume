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
}

export function GlassMetricCard({ title, value, subtitle, icon: Icon, trend, glow, className = '', children }: GlassMetricCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      whileHover={{ y: -3, transition: { duration: 0.2 } }}
      className={`${glow ? 'glass-card-glow' : 'glass-card'} p-5 hover-lift ${className}`}
    >
      <div className="flex items-start justify-between mb-3 relative z-10">
        <span className="text-xs font-medium tracking-wider uppercase text-muted-foreground">{title}</span>
        {Icon && (
          <div className="p-2 rounded-lg bg-primary/10">
            <Icon className="w-4 h-4 text-primary" />
          </div>
        )}
      </div>
      <div className="text-3xl font-bold tracking-tight text-foreground relative z-10">{value}</div>
      {subtitle && <p className="text-sm text-muted-foreground mt-1 relative z-10">{subtitle}</p>}
      {trend && (
        <div className="flex items-center gap-1 mt-2 relative z-10">
          <span className={`text-xs font-medium ${trend.value >= 0 ? 'text-success' : 'text-destructive'}`}>
            {trend.value >= 0 ? '+' : ''}{trend.value}{trend.suffix ?? '%'}
          </span>
          <span className="text-xs text-muted-foreground">{trend.label}</span>
        </div>
      )}
      {children && <div className="relative z-10">{children}</div>}
    </motion.div>
  );
}