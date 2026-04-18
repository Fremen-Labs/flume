import { motion } from 'framer-motion';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { useSnapshot } from '@/hooks/useSnapshot';
import { useTelemetry } from '@/hooks/useTelemetry';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { TrendingUp, Clock, Zap, Target, Loader2, Cpu, Activity, ServerCrash, Network } from 'lucide-react';

const COLORS = ['hsl(160,84%,39%)', 'hsl(38,92%,50%)', 'hsl(0,84%,60%)', 'hsl(239,84%,67%)'];

export default function AnalyticsPage() {
  const { data: snapshot, isLoading: isSnapLoading } = useSnapshot();
  const { data: telemetry, isLoading: isTelLoading } = useTelemetry();
  const isLoading = isSnapLoading || isTelLoading;

  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];
  const reviews = snapshot?.reviews ?? [];
  const failures = snapshot?.failures ?? [];

  const done = tasks.filter(t => t.status === 'done').length;
  const running = tasks.filter(t => t.status === 'running').length;
  const planned = tasks.filter(t => t.status === 'planned' || t.status === 'ready').length;
  const blocked = tasks.filter(t => t.status === 'blocked').length;
  const totalFailuresAndBlocked = failures.length + blocked;
  const total = tasks.length;

  const getTokens = (wName: string, dir: 'input' | 'output') => {
    if (!telemetry?.flume_worker_tokens_total) return 0;
    return telemetry.flume_worker_tokens_total
      .filter(t => t.tags['worker_name'] === wName && t.tags['direction'] === dir)
      .reduce((a, b) => a + b.count, 0);
  };

  const byType = ['epic', 'feature', 'story', 'task'].map(type => ({
    name: type,
    count: tasks.filter(t => (t.item_type ?? 'task') === type).length,
  }));

  const byRole = Object.entries(
    workers.reduce<Record<string, number>>((acc, w) => {
      acc[w.role] = (acc[w.role] || 0) + 1;
      return acc;
    }, {}),
  ).map(([name, value]) => ({ name, value }));

  const statusDist = [
    { name: 'Done', value: done, color: COLORS[0] },
    { name: 'Running', value: running, color: COLORS[3] },
    { name: 'Planned', value: planned, color: 'hsl(215,20%,65%)' },
    { name: 'Blocked', value: blocked, color: COLORS[2] },
  ].filter(d => d.value > 0);

  const approvedReviews = reviews.filter(r => r.verdict === 'approved').length;
  const passRate = reviews.length > 0 ? Math.round((approvedReviews / reviews.length) * 100) : 0;
  
  const tm = snapshot?.token_metrics;
  const realSavings = tm?.savings ?? 0;
  const baselineTokens = tm?.baseline_tokens ?? 0;
  const actualTokensSent = tm?.actual_tokens_sent ?? 0;
  const savingsPercent = baselineTokens > 0 ? Math.round((realSavings / baselineTokens) * 100) : 0;
  const estimatedCost = tm?.estimated_cost_usd ?? 0;
  const dollarsSaved = (estimatedCost > 0 && actualTokensSent > 0) ? (estimatedCost / actualTokensSent) * realSavings : 0;
  const historicalBurn = tm?.historical_burn ?? [];
  
  const fmtTokens = (n: number) => n > 1000000 ? `${(n / 1000000).toFixed(1)}M` : (n > 1000 ? `${(n / 1000).toFixed(1)}K` : String(n));

  const nodeLoads = (telemetry?.flume_node_load ?? []).map(l => ({
    name: l.tags['node_id'] || 'unknown',
    load: Math.round(l.value * 100)
  }));
  
  const routingDecisions = Object.entries((telemetry?.flume_routing_decision ?? []).reduce<Record<string, number>>((acc, d) => {
    const strategy = d.tags['strategy'] || 'unknown';
    acc[strategy] = (acc[strategy] || 0) + d.count;
    return acc;
  }, {})).map(([name, value]) => ({ name, value }));

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Analytics</h1>
        <p className="text-sm text-muted-foreground mt-1">Performance metrics and intelligent swarm observability</p>
      </motion.div>

      {isLoading && (
        <div className="flex items-center gap-2 text-muted-foreground py-10">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading Live Analytics…
        </div>
      )}

      {!isLoading && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-8 gap-4 relative z-10">
            <GlassMetricCard title="Total Tasks" value={String(total)} icon={Target} trend={{ value: done, label: `${done} done` }} />
            <GlassMetricCard title="Review Pass Rate" value={`${passRate}%`} icon={TrendingUp} trend={{ value: passRate, label: `${approvedReviews}/${reviews.length} reviews` }} />
            <GlassMetricCard title="Active Workers" value={String(workers.length)} icon={Zap} trend={{ value: 0, label: `${workers.filter(w => w.status !== 'idle').length} busy` }} />
            <GlassMetricCard title="Failure & Blocked" value={String(totalFailuresAndBlocked)} icon={Clock} trend={{ value: failures.length, label: `${failures.length} hard failures` }} />
            
            {/* Live Telemetry Migrated from Telemetry Page */}
            <GlassMetricCard title="Gateway Engines" value={String(telemetry?.flume_active_models?.length ?? 0)} icon={Activity} trend={{ value: telemetry?.flume_active_models?.length ?? 0, label: telemetry?.flume_active_models?.join(", ") || 'No models loaded' }} />
            <GlassMetricCard title="System Memory" value={telemetry ? `${Math.round(telemetry.go_memstats_sys_bytes / 1024 / 1024)}MB` : '0MB'} icon={Cpu} />
            <GlassMetricCard title="AST Savings" value={fmtTokens(realSavings)} icon={TrendingUp} trend={{ value: savingsPercent, label: `$${dollarsSaved.toFixed(2)} saved vs base cost`, suffix: '%' }} />
            <GlassMetricCard title="VRAM Pressure" value={String(telemetry?.flume_vram_pressure_events_total ?? 0)} icon={ServerCrash} trend={{ value: telemetry?.flume_vram_pressure_events_total ?? 0, label: 'Ensemble clamps' }} />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-4 gap-5 relative z-10">
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Node Mesh Distribution</h3>
              {nodeLoads.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No mesh data</div>
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                   <BarChart data={nodeLoads}>
                    <XAxis dataKey="name" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                    <YAxis unit="%" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                    <Tooltip contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }} />
                    <Bar dataKey="load" fill="hsl(160,84%,39%)" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </motion.div>

            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Routing Decisions</h3>
              {routingDecisions.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No routing data</div>
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                   <BarChart data={routingDecisions} layout="vertical">
                    <XAxis type="number" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                    <YAxis dataKey="name" type="category" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} width={80} />
                    <Tooltip contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }} />
                    <Bar dataKey="value" fill="hsl(239,84%,67%)" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </motion.div>

            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Tasks by Type</h3>
              <ResponsiveContainer width="100%" height={180}>
                 <BarChart data={byType}>
                  <XAxis dataKey="name" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                  <YAxis tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                  <Tooltip contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }} />
                  <Bar dataKey="count" fill="hsl(0,84%,60%)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </motion.div>

            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Status Distribution</h3>
              <ResponsiveContainer width="100%" height={180}>
                  <PieChart>
                    <Pie data={statusDist} cx="50%" cy="50%" innerRadius={50} outerRadius={75} paddingAngle={4} dataKey="value">
                      {statusDist.map((entry, index) => <Cell key={index} fill={entry.color} />)}
                    </Pie>
                    <Tooltip contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }} itemStyle={{ color: 'hsl(210,40%,96%)' }} />
                  </PieChart>
              </ResponsiveContainer>
            </motion.div>
          </div>

          <div className="grid grid-cols-1 gap-5 relative z-10">
            {/* Live Token Usage by Worker via Gateway Metrics */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.3 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-1 flex items-center gap-2"><Network className="w-4 h-4 text-emerald-400" /> Live Token Streaming Usage</h3>
              <p className="text-xs text-muted-foreground mb-4">Powered by direct socket measurement from the Gateway</p>
              {workers.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No workers connected</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm text-left">
                    <thead className="text-xs text-muted-foreground border-b border-border/30">
                      <tr>
                        <th className="pb-2 font-medium">Worker Name</th>
                        <th className="pb-2 font-medium">Role</th>
                        <th className="pb-2 font-medium text-right">Input Tokens Streamed</th>
                        <th className="pb-2 font-medium text-right">Output Tokens Streamed</th>
                        <th className="pb-2 font-medium text-right">Active Token Burn</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/20">
                      {workers
                        .map(w => {
                          const i = getTokens(w.name, 'input');
                          const o = getTokens(w.name, 'output');
                          return { w, i, o };
                        })
                        .filter(row => row.i > 0 || row.o > 0 || row.w.status !== 'idle')
                        .sort((a, b) => (b.i + b.o) - (a.i + a.o))
                        .map(({ w, i, o }) => (
                            <tr key={w.name}>
                              <td className="py-2.5 font-medium flex items-center gap-2">
                                <div className={`w-2 h-2 rounded-full ${w.status === 'running' ? 'bg-emerald-400 animate-pulse' : 'bg-muted'}`}></div>
                                {w.name}
                              </td>
                              <td className="py-2.5 text-muted-foreground capitalize">{w.role}</td>
                              <td className="py-2.5 text-right font-mono text-xs">{i.toLocaleString()}</td>
                              <td className="py-2.5 text-right font-mono text-xs">{o.toLocaleString()}</td>
                              <td className="py-2.5 text-right font-mono text-xs text-emerald-400 font-bold w-32">
                                {(i + o).toLocaleString()}
                              </td>
                            </tr>
                        ))}
                      {workers.every(w => getTokens(w.name, 'input') === 0 && getTokens(w.name, 'output') === 0 && w.status === 'idle') && (
                        <tr>
                          <td colSpan={5} className="py-4 text-center text-xs text-muted-foreground">
                            Waiting for live stream token events...
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </motion.div>

            {/* Historical Token Usage by Worker via Elasticsearch Telemetry */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-1 flex items-center gap-2"><Clock className="w-4 h-4 text-primary" /> Historical Worker Token Burn</h3>
              <p className="text-xs text-muted-foreground mb-4">Total tokens burned persistently retrieved from Elasticsearch telemetry</p>
              {historicalBurn.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No historical worker data</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm text-left">
                    <thead className="text-xs text-muted-foreground border-b border-border/30">
                      <tr>
                        <th className="pb-2 font-medium">Worker Name</th>
                        <th className="pb-2 font-medium">Role</th>
                        <th className="pb-2 font-medium text-right">Lifetime Input Tokens</th>
                        <th className="pb-2 font-medium text-right">Lifetime Output Tokens</th>
                        <th className="pb-2 font-medium text-right">Total Tokens</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/20">
                      {historicalBurn
                        .sort((a, b) => (b.input_tokens + b.output_tokens) - (a.input_tokens + a.output_tokens))
                        .map((b) => (
                          <tr key={b.worker_name}>
                            <td className="py-2.5 font-medium text-foreground">{b.worker_name}</td>
                            <td className="py-2.5 text-muted-foreground capitalize">{b.role}</td>
                            <td className="py-2.5 text-right font-mono text-xs">{b.input_tokens.toLocaleString()}</td>
                            <td className="py-2.5 text-right font-mono text-xs">{b.output_tokens.toLocaleString()}</td>
                            <td className="py-2.5 text-right font-mono text-xs text-primary font-bold w-32">
                              {(b.input_tokens + b.output_tokens).toLocaleString()}
                            </td>
                          </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </motion.div>
          </div>
        </>
      )}
    </div>
  );
}
