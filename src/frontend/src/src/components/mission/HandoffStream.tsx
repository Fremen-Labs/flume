import { motion } from 'framer-motion';
import { ArrowRight, CheckCircle2, Clock, XCircle } from 'lucide-react';
import { handoffEvents } from '@/data/mockData';
import type { HandoffEvent } from '@/types';

function timeAgo(ts: string) {
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

const statusIcon = {
  completed: <CheckCircle2 className="w-3 h-3 text-success" />,
  pending: <Clock className="w-3 h-3 text-warning" />,
  rejected: <XCircle className="w-3 h-3 text-destructive" />,
};

interface HandoffStreamProps {
  compact?: boolean;
}

export function HandoffStream({ compact = false }: HandoffStreamProps) {
  const events = compact ? handoffEvents.slice(0, 5) : handoffEvents;

  return (
    <div className="space-y-1">
      {events.map((evt, i) => (
        <motion.div
          key={evt.id}
          initial={{ opacity: 0, x: -6 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04 }}
          className={`flex items-center gap-2 ${compact ? 'py-1.5' : 'py-2 px-2 hover:bg-white/[0.02] rounded-lg'} transition-colors`}
        >
          {statusIcon[evt.status]}
          <div className="flex items-center gap-1 min-w-0 flex-1">
            <span className="text-[10px] font-medium text-primary truncate">{evt.fromAgentName}</span>
            <ArrowRight className="w-2.5 h-2.5 text-muted-foreground/40 flex-shrink-0" />
            <span className="text-[10px] font-medium text-primary truncate">{evt.toAgentName}</span>
          </div>
          {!compact && (
            <span className="text-[10px] text-muted-foreground truncate max-w-[140px]">{evt.workItemTitle}</span>
          )}
          <span className="text-[9px] text-muted-foreground/60 whitespace-nowrap flex-shrink-0">{timeAgo(evt.timestamp)}</span>
        </motion.div>
      ))}
    </div>
  );
}