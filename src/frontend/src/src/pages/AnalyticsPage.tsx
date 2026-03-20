import { motion } from 'framer-motion';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { useSnapshot } from '@/hooks/useSnapshot';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { TrendingUp, Clock, Zap, Target, Loader2 } from 'lucide-react';

const COLORS = ['hsl(160,84%,39%)', 'hsl(38,92%,50%)', 'hsl(0,84%,60%)', 'hsl(239,84%,67%)'];

export default function AnalyticsPage() {
  const { data: snapshot, isLoading } = useSnapshot();

  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];
  const reviews = snapshot?.reviews ?? [];
  const failures = snapshot?.failures ?? [];

  const done = tasks.filter(t => t.status === 'done').length;
  const running = tasks.filter(t => t.status === 'running').length;
  const planned = tasks.filter(t => t.status === 'planned' || t.status === 'ready').length;
  const blocked = tasks.filter(t => t.status === 'blocked').length;
  const total = tasks.length;

  // Tasks by item type
  const byType = ['epic', 'feature', 'story', 'task'].map(type => ({
    name: type,
    count: tasks.filter(t => (t.item_type ?? 'task') === type).length,
  }));

  // Workers by role
  const byRole = Object.entries(
    workers.reduce<Record<string, number>>((acc, w) => {
      acc[w.role] = (acc[w.role] || 0) + 1;
      return acc;
    }, {}),
  ).map(([name, value]) => ({ name, value }));

  // Status distribution
  const statusDist = [
    { name: 'Done', value: done, color: COLORS[0] },
    { name: 'Running', value: running, color: COLORS[3] },
    { name: 'Planned', value: planned, color: 'hsl(215,20%,65%)' },
    { name: 'Blocked', value: blocked, color: COLORS[2] },
  ].filter(d => d.value > 0);

  const approvedReviews = reviews.filter(r => r.verdict === 'approved').length;
  const passRate = reviews.length > 0 ? Math.round((approvedReviews / reviews.length) * 100) : 0;
  
  const elastro_savings = snapshot?.elastro_savings ?? 0;

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Analytics</h1>
        <p className="text-sm text-muted-foreground mt-1">Performance metrics and system health</p>
      </motion.div>

      {isLoading && (
        <div className="flex items-center gap-2 text-muted-foreground py-10">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading…
        </div>
      )}

      {!isLoading && (
        <>
          {/* Top metrics */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 relative z-10">
            <GlassMetricCard title="Total Tasks" value={String(total)} icon={Target} trend={{ value: done, label: `${done} done` }} />
            <GlassMetricCard title="Review Pass Rate" value={`${passRate}%`} icon={TrendingUp} trend={{ value: passRate, label: `${approvedReviews}/${reviews.length} reviews` }} />
            <GlassMetricCard title="Active Workers" value={String(workers.length)} icon={Zap} trend={{ value: 0, label: `${workers.filter(w => w.status !== 'idle').length} busy` }} />
            <GlassMetricCard title="Failure Count" value={String(failures.length)} icon={Clock} trend={{ value: failures.length, label: 'since start' }} />
            <GlassMetricCard title="AST Tokens Saved" value={elastro_savings > 1000000 ? `${(elastro_savings / 1000000).toFixed(1)}M` : (elastro_savings > 1000 ? `${(elastro_savings / 1000).toFixed(1)}K` : String(elastro_savings))} icon={TrendingUp} trend={{ value: elastro_savings, label: 'efficiency via Elastro' }} />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 relative z-10">
            {/* Tasks by type */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Tasks by Type</h3>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={byType}>
                  <XAxis dataKey="name" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                  <YAxis tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                  <Tooltip
                    contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }}
                    labelStyle={{ color: 'hsl(210,40%,96%)' }}
                  />
                  <Bar dataKey="count" fill="hsl(239,84%,67%)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </motion.div>

            {/* Status distribution */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Status Distribution</h3>
              {statusDist.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No data</div>
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                  <PieChart>
                    <Pie data={statusDist} cx="50%" cy="50%" innerRadius={50} outerRadius={75} paddingAngle={4} dataKey="value">
                      {statusDist.map((entry, index) => (
                        <Cell key={index} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }}
                      labelStyle={{ color: 'hsl(210,40%,96%)' }}
                      itemStyle={{ color: 'hsl(210,40%,96%)' }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              )}
              <div className="flex flex-wrap gap-3 mt-2 justify-center">
                {statusDist.map(d => (
                  <div key={d.name} className="flex items-center gap-1 text-[10px] text-muted-foreground">
                    <span className="w-2 h-2 rounded-full" style={{ background: d.color }} />
                    {d.name} ({d.value})
                  </div>
                ))}
              </div>
            </motion.div>

            {/* Workers by role */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2 }} className="glass-card p-5">
              <h3 className="text-sm font-semibold text-foreground mb-4">Workers by Role</h3>
              {byRole.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No workers</div>
              ) : (
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={byRole} layout="vertical">
                    <XAxis type="number" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} />
                    <YAxis dataKey="name" type="category" tick={{ fill: 'hsl(215,20%,65%)', fontSize: 10 }} width={90} />
                    <Tooltip
                      contentStyle={{ background: 'hsl(222,47%,8%)', border: '1px solid hsl(215,28%,17%)', borderRadius: 8, fontSize: 12 }}
                    />
                    <Bar dataKey="value" fill="hsl(160,84%,39%)" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </motion.div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 relative z-10">
            {/* Token Usage by Worker */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }} className="glass-card p-5 lg:col-span-2">
              <h3 className="text-sm font-semibold text-foreground mb-4">Token Usage by Worker</h3>
              {workers.length === 0 ? (
                <div className="text-xs text-muted-foreground text-center py-8">No workers</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm text-left">
                    <thead className="text-xs text-muted-foreground border-b border-border/30">
                      <tr>
                        <th className="pb-2 font-medium">Worker Name</th>
                        <th className="pb-2 font-medium">Role</th>
                        <th className="pb-2 font-medium text-right">Input Tokens</th>
                        <th className="pb-2 font-medium text-right">Output Tokens</th>
                        <th className="pb-2 font-medium text-right">Total Tokens</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/20">
                      {workers
                        .filter(w => (w.input_tokens || w.output_tokens))
                        .sort((a, b) => ((b.input_tokens || 0) + (b.output_tokens || 0)) - ((a.input_tokens || 0) + (a.output_tokens || 0)))
                        .map(w => {
                          const i = w.input_tokens || 0;
                          const o = w.output_tokens || 0;
                          return (
                            <tr key={w.name}>
                              <td className="py-2.5 font-medium">{w.name}</td>
                              <td className="py-2.5 text-muted-foreground capitalize">{w.role}</td>
                              <td className="py-2.5 text-right font-mono text-xs">{i.toLocaleString()}</td>
                              <td className="py-2.5 text-right font-mono text-xs">{o.toLocaleString()}</td>
                              <td className="py-2.5 text-right font-mono text-xs text-emerald-500/90 w-32">
                                {(i + o).toLocaleString()}
                              </td>
                            </tr>
                          );
                        })}
                      {workers.filter(w => (w.input_tokens || w.output_tokens)).length === 0 && (
                        <tr>
                          <td colSpan={5} className="py-4 text-center text-xs text-muted-foreground">
                            No token telemetry recorded yet. Trigger tasks to see usage.
                          </td>
                        </tr>
                      )}
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
