import { motion } from 'framer-motion';
import { agents, projects } from '@/data/mockData';

export function SwarmView() {
  // Group agents by their current project
  const projectClusters = projects.slice(0, 4).map(project => {
    const projectAgents = agents.filter(a => a.currentProjectId === project.id);
    return { project, agents: projectAgents };
  });

  return (
    <div className="relative min-h-[300px]">
      {/* Grid background */}
      <div
        className="absolute inset-0 opacity-[0.03] rounded-xl"
        style={{
          backgroundImage: 'radial-gradient(circle, rgba(255,255,255,0.3) 1px, transparent 1px)',
          backgroundSize: '20px 20px',
        }}
      />

      <div className="grid grid-cols-2 gap-4 relative z-10">
        {projectClusters.map((cluster, ci) => {
          const intensity = cluster.agents.length / 4;
          return (
            <motion.div
              key={cluster.project.id}
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: ci * 0.1 }}
              className="glass-surface rounded-xl p-4 relative overflow-hidden"
            >
              {/* Glow based on intensity */}
              <div
                className="absolute inset-0 rounded-xl pointer-events-none"
                style={{
                  background: `radial-gradient(circle at center, hsl(239 84% 67% / ${Math.min(intensity * 0.08, 0.12)}), transparent 70%)`,
                }}
              />

              <div className="relative z-10">
                <div className="flex items-center justify-between mb-3">
                  <h4 className="text-xs font-semibold text-foreground">{cluster.project.name}</h4>
                  <span className="text-[10px] text-muted-foreground">{cluster.agents.length} agents</span>
                </div>

                {/* Agent dots */}
                <div className="flex flex-wrap gap-2">
                  {cluster.agents.map((agent, ai) => {
                    const isActive = agent.status === 'active';
                    const isBlocked = agent.status === 'blocked';
                    return (
                      <motion.div
                        key={agent.id}
                        initial={{ scale: 0 }}
                        animate={{ scale: 1 }}
                        transition={{ delay: ci * 0.1 + ai * 0.05, type: 'spring' }}
                        className="group relative"
                      >
                        <div
                          className={`w-6 h-6 rounded-full flex items-center justify-center text-[8px] font-bold cursor-default transition-all
                            ${isActive ? 'bg-primary/20 text-primary ring-1 ring-primary/30' : 
                              isBlocked ? 'bg-destructive/20 text-destructive ring-1 ring-destructive/30' :
                              'bg-muted/40 text-muted-foreground ring-1 ring-white/[0.06]'}`}
                          style={{ animation: isActive ? 'breathing 3s ease-in-out infinite' : 'none' }}
                        >
                          {agent.name.charAt(0)}
                        </div>
                        {/* Tooltip on hover */}
                        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 glass-card rounded text-[9px] text-foreground whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-20">
                          {agent.name}
                        </div>
                      </motion.div>
                    );
                  })}
                  {cluster.agents.length === 0 && (
                    <span className="text-[10px] text-muted-foreground/50 italic">No active agents</span>
                  )}
                </div>

                {/* Activity pulse */}
                {cluster.agents.filter(a => a.status === 'active').length > 0 && (
                  <div className="mt-3 flex items-center gap-1.5">
                    <div className="h-px flex-1 bg-gradient-to-r from-primary/20 via-primary/40 to-primary/20 rounded" />
                    <span className="text-[9px] text-primary/60">
                      {cluster.agents.filter(a => a.status === 'active').length} active streams
                    </span>
                    <div className="h-px flex-1 bg-gradient-to-r from-primary/20 via-primary/40 to-primary/20 rounded" />
                  </div>
                )}
              </div>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}