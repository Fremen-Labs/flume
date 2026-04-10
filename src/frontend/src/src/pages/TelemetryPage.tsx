import { motion } from 'framer-motion';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import { useTelemetry } from '@/hooks/useTelemetry';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { ServerCog, Activity, Cpu, AlertTriangle, Loader2, ThermometerSun, Database, Code2 } from 'lucide-react';

const COLORS = ['hsl(160,84%,39%)', 'hsl(38,92%,50%)', 'hsl(0,84%,60%)', 'hsl(239,84%,67%)'];

export default function TelemetryPage() {
  const { data: telemetry, isLoading, error } = useTelemetry();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center p-20 text-muted-foreground gap-2">
        <Loader2 className="w-5 h-5 animate-spin" /> Fetching Gateway Telemetry...
      </div>
    );
  }

  if (error || !telemetry) {
    return (
      <div className="flex flex-col items-center justify-center p-20 text-destructive gap-4">
        <ServerCog className="w-10 h-10" />
        <h3 className="font-semibold text-lg">Telemetry Gateway Unreachable</h3>
        <p className="text-sm text-muted-foreground">Ensure the Flume backend and gateway are running.</p>
      </div>
    );
  }

  // Formatting memory helpers
  const formatMB = (bytes: number) => (bytes / 1024 / 1024).toFixed(1) + ' MB';
  
  const allocMem = formatMB(telemetry.go_memstats_alloc_bytes);
  const sysMem = formatMB(telemetry.go_memstats_sys_bytes);

  // Group ensemble requests by model_family for chart
  const modelDist = telemetry.flume_ensemble_requests_total.reduce((acc, curr) => {
    const family = curr.tags['model_family'] || 'unknown';
    const existing = acc.find(item => item.name === family);
    if (existing) {
      existing.requests += curr.count;
    } else {
      acc.push({ name: family, requests: curr.count });
    }
    return acc;
  }, [] as { name: string; requests: number }[]);

  const totalEnsembles = telemetry.flume_ensemble_requests_total.reduce((acc, curr) => acc + curr.count, 0);

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10 flex items-center gap-3">
        <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center border border-primary/20 breathing">
            <ServerCog className="w-5 h-5 text-primary" />
        </div>
        <div>
           <h1 className="text-2xl font-bold tracking-tight text-foreground flex items-center gap-2">
             Engineering Telemetry
             {telemetry.flume_up === 1 ? (
               <span className="flex h-2 w-2 relative">
                 <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                 <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
               </span>
             ) : (
               <span className="w-2 h-2 rounded-full bg-destructive"></span>
             )}
           </h1>
           <p className="text-sm text-primary/70 mt-1 font-mono">Gateway Matrix Native v{telemetry.flume_build_info}</p>
        </div>
      </motion.div>

      {/* Core Resources */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 relative z-10">
        <GlassMetricCard 
            title="Go Goroutines" 
            value={telemetry.go_goroutines.toLocaleString()} 
            icon={Activity} 
            trend={{ value: 0, label: 'internal threads' }} 
        />
        <GlassMetricCard 
            title="Resident Memory" 
            value={allocMem} 
            icon={Cpu} 
            trend={{ value: 0, label: `sys limit: ${sysMem}` }} 
        />
        <GlassMetricCard 
            title="VRAM Constraints" 
            value={telemetry.flume_vram_pressure_events_total.toLocaleString()} 
            icon={ThermometerSun} 
            trend={{ value: telemetry.flume_vram_pressure_events_total, label: 'drops to prevent OOM' }} 
        />
        <GlassMetricCard 
            title="Boundary Escalations" 
            value={telemetry.flume_escalation_total.toLocaleString()} 
            icon={AlertTriangle} 
            trend={{ value: telemetry.flume_escalation_total, label: 'fallback to frontier limits' }} 
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 relative z-10">
        
        {/* Model Ensemble Load */}
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="glass-card p-5 lg:col-span-2">
          <h3 className="text-sm font-semibold text-foreground mb-1 flex items-center gap-2">
            <Database className="w-4 h-4 text-muted-foreground" />
            Adaptive Jury Distribution
          </h3>
          <p className="text-xs text-muted-foreground mb-4">Total ensemble executions dispersed dynamically by the active model family.</p>
          
          {modelDist.length === 0 ? (
            <div className="text-xs text-muted-foreground text-center py-12">Waiting for first ensemble execution payload...</div>
          ) : (
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={modelDist} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(215,20%,20%)" vertical={false} />
                <XAxis dataKey="name" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 11 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: 'hsl(215,20%,65%)', fontSize: 11 }} axisLine={false} tickLine={false} />
                <Tooltip
                  contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: 'hsl(210,40%,96%)', marginBottom: 4 }}
                  cursor={{ fill: 'hsl(215,28%,15%)' }}
                />
                <Bar dataKey="requests" fill="url(#colorPrimary)" radius={[4, 4, 0, 0]} maxBarSize={60} />
                <defs>
                  <linearGradient id="colorPrimary" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="hsl(239,84%,67%)" stopOpacity={0.8} />
                    <stop offset="100%" stopColor="hsl(239,84%,67%)" stopOpacity={0.2} />
                  </linearGradient>
                </defs>
              </BarChart>
            </ResponsiveContainer>
          )}
        </motion.div>

        {/* Info / Diagnostics Module */}
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }} className="glass-card p-5 overflow-hidden relative">
          <div className="absolute -right-4 -top-4 w-24 h-24 bg-primary/10 rounded-full blur-2xl" />
          
          <h3 className="text-sm font-semibold text-foreground mb-4 flex items-center gap-2">
             <Code2 className="w-4 h-4 text-muted-foreground" />
             Active Subsystem Diagnoses
          </h3>
          
          <div className="space-y-4">
             <div className="p-3 bg-card/60 border border-border/50 rounded-lg">
                 <div className="text-xs text-muted-foreground uppercase opacity-80 font-bold mb-1 tracking-wider">Gateway Native Engine</div>
                 <div className="text-sm font-medium flex justify-between">
                     <span>Flume v{telemetry.flume_build_info}</span>
                     <span className="text-emerald-400">Online</span>
                 </div>
             </div>
             
             <div className="p-3 bg-card/60 border border-border/50 rounded-lg">
                 <div className="text-xs text-muted-foreground uppercase opacity-80 font-bold mb-1 tracking-wider">Local Vector Load</div>
                 <div className="text-sm font-medium flex justify-between">
                     <span>Active Evaluators</span>
                     <span className="text-primary">{telemetry.flume_active_models.length} Models</span>
                 </div>
                 <div className="flex flex-wrap gap-1 mt-2">
                     {telemetry.flume_active_models.map(m => (
                         <span key={m} className="px-2 py-0.5 rounded-sm bg-primary/20 text-primary text-[10px] font-mono border border-primary/30">
                           {m}
                         </span>
                     ))}
                     {telemetry.flume_active_models.length === 0 && <span className="text-xs text-muted-foreground">Idle</span>}
                 </div>
             </div>

             <div className="p-3 bg-card/60 border border-border/50 rounded-lg">
                 <div className="text-xs text-muted-foreground uppercase opacity-80 font-bold mb-1 tracking-wider">Cumulus Ensemble Hits</div>
                 <div className="text-2xl font-mono mt-1 text-foreground">
                    {totalEnsembles.toLocaleString()}
                 </div>
                 <div className="text-xs text-muted-foreground mt-1">Total juries successfully spawned</div>
             </div>
          </div>
        </motion.div>

      </div>
    </div>
  );
}
