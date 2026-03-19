import { motion } from 'framer-motion';
import { throughputHeatmap } from '@/data/mockData';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

const stages = ['Intake', 'Breakdown', 'Architecture', 'Story Writing', 'Coding', 'Review', 'QA', 'Deploy'];
const periods = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function getHeatColor(value: number, max: number): string {
  const ratio = value / max;
  if (ratio < 0.15) return 'rgba(99, 102, 241, 0.08)';
  if (ratio < 0.3) return 'rgba(99, 102, 241, 0.15)';
  if (ratio < 0.5) return 'rgba(99, 102, 241, 0.25)';
  if (ratio < 0.7) return 'rgba(52, 211, 153, 0.3)';
  if (ratio < 0.85) return 'rgba(52, 211, 153, 0.45)';
  return 'rgba(52, 211, 153, 0.6)';
}

interface ThroughputHeatmapProps {
  compact?: boolean;
}

export function ThroughputHeatmap({ compact = false }: ThroughputHeatmapProps) {
  const maxVal = Math.max(...throughputHeatmap.map(c => c.value));
  const displayStages = compact ? stages.slice(0, 5) : stages;
  const displayPeriods = compact ? periods.slice(0, 5) : periods;

  return (
    <div className="relative z-10">
      <div className="overflow-x-auto">
        <table className="w-full border-separate" style={{ borderSpacing: compact ? '2px' : '3px' }}>
          <thead>
            <tr>
              <th className="text-[10px] text-muted-foreground text-left font-medium pr-2 w-20" />
              {displayPeriods.map(p => (
                <th key={p} className="text-[10px] text-muted-foreground font-medium text-center">{p}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayStages.map((stage, si) => (
              <tr key={stage}>
                <td className="text-[10px] text-muted-foreground pr-2 whitespace-nowrap">{stage}</td>
                {displayPeriods.map((period, pi) => {
                  const cell = throughputHeatmap.find(c => c.stage === stage && c.period === period);
                  const value = cell?.value || 0;
                  return (
                    <td key={period}>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <motion.div
                            initial={{ opacity: 0, scale: 0.8 }}
                            animate={{ opacity: 1, scale: 1 }}
                            transition={{ delay: (si * displayPeriods.length + pi) * 0.01 }}
                            className={`${compact ? 'h-5 min-w-[20px]' : 'h-7 min-w-[32px]'} rounded cursor-default transition-all hover:ring-1 hover:ring-primary/30`}
                            style={{ backgroundColor: getHeatColor(value, maxVal) }}
                          />
                        </TooltipTrigger>
                        <TooltipContent className="glass-card border border-white/10 text-xs">
                          <p className="font-medium">{stage} · {period}</p>
                          <p className="text-muted-foreground">{value} items processed</p>
                        </TooltipContent>
                      </Tooltip>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!compact && (
        <div className="flex items-center gap-2 mt-3 text-[10px] text-muted-foreground">
          <span>Less</span>
          {[0.08, 0.15, 0.25, 0.35, 0.45, 0.6].map((op, i) => (
            <div key={i} className="w-3 h-3 rounded" style={{ backgroundColor: i < 3 ? `rgba(99,102,241,${op})` : `rgba(52,211,153,${op})` }} />
          ))}
          <span>More</span>
        </div>
      )}
    </div>
  );
}