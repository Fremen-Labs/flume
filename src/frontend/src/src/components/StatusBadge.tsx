import { motion } from 'framer-motion';

const statusConfig: Record<string, { color: string; label: string; dotClass: string }> = {
  // Work item statuses (legacy)
  backlog: { color: 'text-muted-foreground', label: 'Backlog', dotClass: 'bg-muted-foreground' },
  intake: { color: 'text-muted-foreground', label: 'Intake', dotClass: 'bg-muted-foreground' },
  breakdown: { color: 'text-secondary', label: 'Breakdown', dotClass: 'bg-secondary' },
  architecture: { color: 'text-primary', label: 'Architecture', dotClass: 'bg-primary' },
  story_writing: { color: 'text-primary', label: 'Story Writing', dotClass: 'bg-primary' },
  task_generation: { color: 'text-primary', label: 'Task Gen', dotClass: 'bg-primary' },
  in_progress: { color: 'text-primary', label: 'In Progress', dotClass: 'bg-primary' },
  code_review: { color: 'text-warning', label: 'Code Review', dotClass: 'bg-warning' },
  qa: { color: 'text-warning', label: 'QA', dotClass: 'bg-warning' },
  done: { color: 'text-success', label: 'Done', dotClass: 'bg-success' },
  blocked: { color: 'text-destructive', label: 'Blocked', dotClass: 'bg-destructive' },
  // Real API task statuses
  planned: { color: 'text-muted-foreground', label: 'Planned', dotClass: 'bg-muted-foreground' },
  ready: { color: 'text-primary', label: 'Ready', dotClass: 'bg-primary' },
  running: { color: 'text-primary', label: 'Running', dotClass: 'bg-primary' },
  review: { color: 'text-amber-400', label: 'In Review', dotClass: 'bg-amber-400' },
  // Agent / worker statuses
  idle: { color: 'text-muted-foreground', label: 'Idle', dotClass: 'bg-muted-foreground' },
  active: { color: 'text-success', label: 'Active', dotClass: 'bg-success' },
  claimed: { color: 'text-primary', label: 'Claimed', dotClass: 'bg-primary' },
  waiting: { color: 'text-warning', label: 'Waiting', dotClass: 'bg-warning' },
  failed: { color: 'text-destructive', label: 'Failed', dotClass: 'bg-destructive' },
  offline: { color: 'text-muted-foreground', label: 'Offline', dotClass: 'bg-muted-foreground' },
  // Project statuses
  planning: { color: 'text-secondary', label: 'Planning', dotClass: 'bg-secondary' },
  paused: { color: 'text-warning', label: 'Paused', dotClass: 'bg-warning' },
  completed: { color: 'text-success', label: 'Completed', dotClass: 'bg-success' },
  archived: { color: 'text-muted-foreground', label: 'Archived', dotClass: 'bg-muted-foreground' },
  // Health
  healthy: { color: 'text-success', label: 'Healthy', dotClass: 'bg-success' },
  at_risk: { color: 'text-warning', label: 'At Risk', dotClass: 'bg-warning' },
  critical: { color: 'text-destructive', label: 'Critical', dotClass: 'bg-destructive' },
  // Priority
  high: { color: 'text-warning', label: 'High', dotClass: 'bg-warning' },
  medium: { color: 'text-primary', label: 'Medium', dotClass: 'bg-primary' },
  normal: { color: 'text-muted-foreground', label: 'Normal', dotClass: 'bg-muted-foreground' },
  low: { color: 'text-muted-foreground', label: 'Low', dotClass: 'bg-muted-foreground' },
  // PR statuses
  pr_open: { color: 'text-success', label: 'PR Open', dotClass: 'bg-success' },
  pr_merged: { color: 'text-primary', label: 'PR Merged', dotClass: 'bg-primary' },
  pr_failed: { color: 'text-destructive', label: 'PR Failed', dotClass: 'bg-destructive' },
};

export function StatusBadge({ status, pulse }: { status: string; pulse?: boolean }) {
  const config = statusConfig[status] || { color: 'text-muted-foreground', label: status, dotClass: 'bg-muted-foreground' };

  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${config.color}`}>
      <span className="relative flex h-2 w-2">
        {pulse && (status === 'active' || status === 'in_progress') && (
          <motion.span
            className={`absolute inset-0 rounded-full ${config.dotClass} opacity-20`}
            animate={{ scale: [1, 1.35, 1], opacity: [0.2, 0, 0.2] }}
            transition={{ duration: 4.5, ease: 'easeInOut', repeat: Infinity, repeatDelay: 0.6 }}
          />
        )}
        <span className={`relative inline-flex rounded-full h-2 w-2 ${config.dotClass}`} />
      </span>
      {config.label}
    </span>
  );
}
