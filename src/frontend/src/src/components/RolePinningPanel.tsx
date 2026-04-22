import { Brain, Code, Eye, TestTube2, Briefcase, MessageSquare } from 'lucide-react';
import type { FrontierModelWeight } from '@/types';

const AGENT_ROLES = [
  { id: 'planner',     label: 'Planner',      icon: Brain },
  { id: 'implementer', label: 'Implementer',  icon: Code },
  { id: 'reviewer',    label: 'Reviewer',     icon: Eye },
  { id: 'tester',      label: 'Tester',       icon: TestTube2 },
  { id: 'pm',          label: 'PM',           icon: Briefcase },
  { id: 'critic',      label: 'Critic',       icon: MessageSquare },
];

interface Props {
  rolePinning: Record<string, string>;
  frontierMix: FrontierModelWeight[];
  onChange: (pinning: Record<string, string>) => void;
}

export function RolePinningPanel({ rolePinning, frontierMix, onChange }: Props) {
  const availableModels = frontierMix.filter(m => !m.circuit_open);

  const handleChange = (role: string, model: string) => {
    const updated = { ...rolePinning };
    if (model === '' || model === 'auto') {
      delete updated[role];
    } else {
      updated[role] = model;
    }
    onChange(updated);
  };

  return (
    <div className="glass-card p-5 space-y-4" id="role-pinning-panel">
      {/* Header */}
      <div className="flex items-center gap-2">
        <div className="w-8 h-8 rounded-lg bg-amber-500/15 flex items-center justify-center">
          <Brain className="w-4 h-4 text-amber-400" />
        </div>
        <div>
          <p className="text-sm font-semibold text-foreground">Role Assignments</p>
          <p className="text-[10px] text-muted-foreground/70">Pin specific frontier models to agent roles</p>
        </div>
      </div>

      {/* Role grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {AGENT_ROLES.map(({ id, label, icon: Icon }) => {
          const pinned = rolePinning[id] ?? '';
          const isPinned = pinned !== '' && pinned !== 'auto';

          return (
            <div
              key={id}
              className={`rounded-xl border p-3 transition-colors ${
                isPinned
                  ? 'border-amber-500/30 bg-amber-500/[0.04]'
                  : 'border-border/20 bg-white/[0.02]'
              }`}
            >
              <div className="flex items-center gap-2 mb-2">
                <Icon className={`w-4 h-4 ${isPinned ? 'text-amber-400' : 'text-muted-foreground/60'}`} />
                <span className={`text-xs font-medium ${isPinned ? 'text-foreground' : 'text-muted-foreground'}`}>
                  {label}
                </span>
                {isPinned && (
                  <span className="ml-auto px-1.5 py-0.5 rounded text-[9px] bg-amber-500/15 text-amber-400 font-medium">
                    Pinned
                  </span>
                )}
              </div>

              <select
                id={`role-pin-${id}`}
                value={pinned || 'auto'}
                onChange={(e) => handleChange(id, e.target.value)}
                className="w-full bg-white/5 border border-border/20 rounded-lg px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-amber-500/40 appearance-none cursor-pointer"
              >
                <option value="auto">Auto (weighted)</option>
                {availableModels.map(m => (
                  <option key={`${m.provider}-${m.model}`} value={m.model}>
                    {m.model} ({m.provider})
                  </option>
                ))}
              </select>
            </div>
          );
        })}
      </div>
    </div>
  );
}
