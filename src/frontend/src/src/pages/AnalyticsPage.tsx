import { motion } from 'framer-motion';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { useSnapshot } from '@/hooks/useSnapshot';
import { useTelemetry } from '@/hooks/useTelemetry';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { TrendingUp, Clock, Zap, Target, Loader2, Cpu, Activity, ServerCrash, Network, Server, AlertTriangle, HelpCircle } from 'lucide-react';
import { createLogger } from '@/utils/logger';

const COLORS = ['hsl(160,84%,39%)', 'hsl(38,92%,50%)', 'hsl(0,84%,60%)', 'hsl(239,84%,67%)'];

// ─────────────────────────────────────────────────────────────────────────────
// Logger (LogLoom structured) + local types for unified /api/nodes source
// Authoritative loads come from gateway health_checker + node_registry (CurrentLoad
// derived from sum(size_vram) / MemoryGB). Telemetry flume_node_load path is dead.
// ─────────────────────────────────────────────────────────────────────────────
const log = createLogger('pages.AnalyticsPage');

// Minimal shapes matching Node + Health + Capabilities from /api/nodes (via dashboard proxy)
interface NodeHealth {
  current_load?: number;
  status?: string;
  loaded_models?: string[];
  last_seen?: string;
  latency_ms?: number;
}
interface NodeCapabilities {
  memory_gb?: number;
  reasoning_score?: number;
  max_context?: number;
  quantization?: string;
  estimated_tps?: number;
}
interface ApiNode {
  id: string;
  host?: string;
  model_tag?: string;
  capabilities?: NodeCapabilities;
  health?: NodeHealth;
}
interface NodesApiResponse {
  nodes?: ApiNode[];
  count?: number;
  error?: string; // set by dashboard when gateway unreachable (partial resilience)
}

interface NodeChartDatum {
  name: string;
  load: number;
  loadRaw: number;
  memoryGB: number;
  usedGB: number;
  modelCount: number;
  status: string;
}

// Stable custom tooltip renderer (hoisted to avoid per-render recreation for Recharts)
function NodeLoadTooltip({ active, payload }: { active?: boolean; payload?: { payload?: NodeChartDatum }[] }) {
  if (!active || !payload?.length) return null;
  const d = (payload[0].payload || {}) as NodeChartDatum;
  const used = typeof d.usedGB === 'number' ? d.usedGB.toFixed(1) : '?';
  const total = typeof d.memoryGB === 'number' && d.memoryGB > 0 ? d.memoryGB : '?';
  return (
    <div className="rounded-lg border border-[hsl(215,28%,17%)] bg-[hsl(222,47%,8%)] p-3 text-xs shadow-xl">
      <div className="font-semibold text-foreground mb-1">{d.name || 'node'}</div>
      <div className="text-muted-foreground">
        Load: <span className="font-mono text-foreground">{d.load ?? 0}%</span> ({used} / {total} GB VRAM)
      </div>
      <div className="text-muted-foreground">Models loaded: <span className="font-mono text-foreground">{d.modelCount ?? 0}</span></div>
      <div className="text-muted-foreground">Status: <span className="font-mono text-foreground capitalize">{d.status || 'unknown'}</span></div>
      <div className="mt-1 text-[10px] text-muted-foreground/70">Lower load preferred for routing</div>
    </div>
  );
}

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
  const inReview = tasks.filter(t => t.status === 'review').length;
  const planned = tasks.filter(t => t.status === 'planned' || t.status === 'ready').length;
  const blocked = tasks.filter(t => t.status === 'blocked').length;
  const totalFailuresAndBlocked = failures.length + blocked;

  // Grok uplift: prefer efficient backend task_count (Total Tasks card).
  // Falls back to tasks.length for backward compat during rollout.
  const total = snapshot?.task_count ?? tasks.length;

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
    { name: 'In Review', value: inReview, color: 'hsl(265,70%,60%)' },
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

  // ── Unified real data source: /api/nodes (NodeRegistry + HealthChecker) ──
  // Replaces dead telemetry?.flume_node_load. Shares react-query cache with NodesOverview.
  const { data: nodesData, isLoading: isNodesLoading, isError: isNodesError, error: nodesQueryError } = useQuery<NodesApiResponse>({
    queryKey: ['nodes'],
    queryFn: async () => {
      log.debug('fetchNodes', 'Fetching fresh /api/nodes for Node Mesh Distribution chart');
      try {
        const res = await fetch('/api/nodes');
        if (!res.ok) {
          log.warn('fetchNodes', `Non-OK response from /api/nodes`, { status: res.status });
          throw new Error(`HTTP ${res.status}`);
        }
        const json = await res.json();
        log.info('fetchNodes', 'Node mesh data arrived for Analytics', {
          nodeCount: json?.nodes?.length ?? 0,
          gatewayError: json?.error || null,
        });
        return json as NodesApiResponse;
      } catch (e) {
        log.error('fetchNodes', 'Failed to fetch /api/nodes for chart (will show resilient state)', { error: String(e) });
        throw e;
      }
    },
    refetchInterval: 15_000,
    staleTime: 10_000,
    retry: 1,
  });

  const gatewayError = nodesData?.error;
  const rawNodes: ApiNode[] = nodesData?.nodes ?? [];
  const nodeChartData = rawNodes
    .map((n) => {
      const load = n.health?.current_load ?? 0; // 0.0–1.0 authoritative from health_checker
      const memGB = n.capabilities?.memory_gb ?? 0;
      const usedGB = load * memGB;
      return {
        name: n.id || 'unknown',
        load: Math.round(load * 100),
        loadRaw: load,
        memoryGB: memGB,
        usedGB: usedGB,
        modelCount: n.health?.loaded_models?.length ?? 0,
        status: n.health?.status || 'unknown',
      };
    })
    .sort((a, b) => b.load - a.load); // sort desc by load (actionable: highest pressure first)

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
            {/* Node Mesh Distribution — now unified on real /api/nodes (health.current_load) */}
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="glass-card p-5">
              <div className="mb-3">
                <h3 className="text-sm font-semibold text-foreground flex items-center gap-2">
                  Node Mesh Distribution
                  <span title="VRAM memory pressure (sum of loaded models' size_vram ÷ node's declared MemoryGB). Lower is preferred for routing. Data from health_checker probes.">
                    <HelpCircle className="w-3.5 h-3.5 text-muted-foreground/60 hover:text-muted-foreground transition-colors" />
                  </span>
                </h3>
                <p className="text-[10px] leading-snug text-muted-foreground mt-0.5">
                  VRAM memory pressure (sum of loaded models' size_vram ÷ node's declared MemoryGB). Lower is preferred for routing.
                </p>
              </div>

              {isNodesLoading ? (
                <div className="flex h-[170px] items-center justify-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading node loads from registry…
                </div>
              ) : (isNodesError || gatewayError) && nodeChartData.length === 0 ? (
                <div className="flex h-[170px] flex-col items-center justify-center gap-2 rounded-md border border-amber-500/20 bg-amber-500/5 p-4 text-center text-xs">
                  <AlertTriangle className="h-6 w-6 text-amber-400" />
                  <div className="font-medium text-amber-300">Gateway unreachable</div>
                  <div className="text-amber-400/80">Last known loads unavailable. Check Node Mesh or gateway health.</div>
                </div>
              ) : nodeChartData.length === 0 ? (
                <div className="flex h-[170px] flex-col items-center justify-center gap-2 text-center">
                  <Server className="h-8 w-8 text-muted-foreground/40" />
                  <p className="text-xs text-muted-foreground">No nodes registered in the mesh</p>
                  <Link
                    to="/nodes"
                    className="inline-flex items-center gap-1 rounded-md border border-border/30 px-2.5 py-1 text-[10px] text-primary hover:bg-white/5 hover:text-primary/90 transition-colors"
                  >
                    Go to Node Mesh → Register nodes
                  </Link>
                </div>
              ) : (
                <ResponsiveContainer width="100%" height={170}>
                  <BarChart data={nodeChartData} margin={{ top: 4, right: 4, left: -4, bottom: 0 }}>
                    <XAxis
                      dataKey="name"
                      tick={{ fill: 'hsl(215,20%,65%)', fontSize: 9 }}
                      tickLine={{ stroke: 'hsl(215,28%,17%)' }}
                    />
                    <YAxis
                      unit="%"
                      domain={[0, 100]}
                      tick={{ fill: 'hsl(215,20%,65%)', fontSize: 9 }}
                      tickLine={{ stroke: 'hsl(215,28%,17%)' }}
                    />
                    <Tooltip content={<NodeLoadTooltip />} cursor={{ fill: 'hsl(215,20%,65%,0.08)' }} />
                    <Bar dataKey="load" radius={[4, 4, 0, 0]}>
                      {nodeChartData.map((entry, index) => {
                        const fill = entry.load >= 80
                          ? 'hsl(0,84%,60%)'   // high pressure — red
                          : entry.load >= 55
                          ? 'hsl(38,92%,50%)'  // medium — amber
                          : 'hsl(160,84%,39%)'; // healthy — emerald
                        return <Cell key={`cell-${index}`} fill={fill} />;
                      })}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              )}

              {/* Partial / resilience footer */}
              {gatewayError && nodeChartData.length > 0 && (
                <div className="mt-2 rounded border border-amber-500/20 bg-amber-500/5 px-2 py-1 text-[10px] text-amber-400">
                  Partial data — gateway unreachable (showing last cached loads)
                </div>
              )}
              {nodeChartData.length > 0 && (
                <div className="mt-1.5 text-right text-[9px] text-muted-foreground/60">
                  {nodeChartData.length} node{nodeChartData.length === 1 ? '' : 's'} • sorted by load desc
                </div>
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
