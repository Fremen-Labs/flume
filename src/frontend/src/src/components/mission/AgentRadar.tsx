import { motion } from 'framer-motion';
import { agents } from '@/data/mockData';
import { StatusBadge } from '@/components/StatusBadge';
import agentAvatar1 from '@/assets/agents/agent-1.png';
import agentAvatar2 from '@/assets/agents/agent-2.png';
import agentAvatar3 from '@/assets/agents/agent-3.png';
import agentAvatar4 from '@/assets/agents/agent-4.png';

const agentAvatars = [agentAvatar1, agentAvatar2, agentAvatar3, agentAvatar4];

interface AgentRadarProps {
  compact?: boolean;
}

export function AgentRadar({ compact = false }: AgentRadarProps) {
  const displayAgents = compact ? agents.filter(a => a.status === 'active').slice(0, 6) : agents;

  return (
    <div className={compact ? 'space-y-2' : 'space-y-3'}>
      {!compact && (
        <div className="grid grid-cols-4 gap-2 mb-4">
          {['active', 'idle', 'waiting', 'blocked'].map(status => {
            const count = agents.filter(a => a.status === status).length;
            return (
              <div key={status} className="glass-surface rounded-lg p-2 text-center">
                <div className="text-lg font-bold text-foreground">{count}</div>
                <div className="text-[10px] text-muted-foreground capitalize">{status}</div>
              </div>
            );
          })}
        </div>
      )}

      <div className={compact ? 'space-y-1.5' : 'grid grid-cols-1 md:grid-cols-2 gap-3'}>
        {displayAgents.map((agent, i) => {
          const intensityClass = agent.utilization >= 80 ? 'ring-success/40' :
            agent.utilization >= 40 ? 'ring-primary/30' : 'ring-muted/20';
          const pulseSpeed = agent.status === 'active' ? '2s' : '5s';

          return (
            <motion.div
              key={agent.id}
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: i * 0.03 }}
              className={`flex items-center gap-3 ${compact ? 'py-1' : 'glass-surface rounded-lg p-3'} group`}
            >
              <div className="relative flex-shrink-0">
                <div
                  className={`w-8 h-8 rounded-full overflow-hidden ring-2 ${intensityClass}`}
                  style={{ animation: agent.status === 'active' ? `breathing ${pulseSpeed} ease-in-out infinite` : 'none' }}
                >
                  <img src={agentAvatars[i % agentAvatars.length]} alt="" className="w-full h-full object-cover" />
                </div>
                {agent.status === 'active' && (
                  <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-success border-2 border-card" />
                )}
                {agent.status === 'blocked' && (
                  <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-destructive border-2 border-card" />
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5">
                  <span className="text-xs font-semibold text-foreground truncate">{agent.name}</span>
                  <StatusBadge status={agent.status} pulse={agent.status === 'active'} />
                </div>
                <p className="text-[10px] text-muted-foreground truncate">
                  {agent.currentTaskTitle || agent.specialty}
                </p>
                {!compact && (
                  <div className="flex items-center gap-3 mt-1">
                    <span className="text-[10px] text-muted-foreground">{agent.utilization}% util</span>
                    <span className="text-[10px] text-muted-foreground">Q:{agent.queueDepth}</span>
                    <span className="text-[10px] text-success">{agent.successRate}%</span>
                  </div>
                )}
              </div>
              {!compact && (
                <div className="w-12 h-1.5 rounded-full bg-white/[0.05] overflow-hidden">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${agent.utilization}%` }}
                    transition={{ duration: 0.8, delay: i * 0.03 }}
                    className={`h-full rounded-full ${
                      agent.utilization >= 80 ? 'bg-success' : agent.utilization >= 40 ? 'bg-primary' : 'bg-muted-foreground'
                    }`}
                  />
                </div>
              )}
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}