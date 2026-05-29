import { motion } from 'framer-motion';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts';
import { useSnapshot } from '@/hooks/useSnapshot';
import { useTelemetry } from '@/hooks/useTelemetry';
import { useSystemState } from '@/hooks/useSystemState';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { TrendingUp, Clock, Zap, Target, Loader2, Cpu, Activity, ServerCrash, Network, Gauge, Info, Braces } from 'lucide-react';
import { createLogger } from '@/utils/logger';

const log = createLogger('pages.AnalyticsPage');

const COLORS = ['hsl(160,84%,39%)', 'hsl(38,92%,50%)', 'hsl(0,84%,60%)', 'hsl(239,84%,67%)'];

export default function AnalyticsPage() {
  const { data: snapshot, isLoading: isSnapLoading, error: snapError } = useSnapshot();
  const { data: telemetry, isLoading: isTelLoading, error: telError } = useTelemetry();

  // Refined loading for resilience: cards always render; use per-card loading/error/partial
  const snapLoading = isSnapLoading && !snapshot;
  const telLoading = isTelLoading && !telemetry;
  const isInitialLoading = snapLoading && telLoading;

  const snapErrMsg = snapError ? (snapError instanceof Error ? snapError.message : String(snapError)) : null;
  const telErrMsg = telError ? (telError instanceof Error ? telError.message : String(telError)) : null;

  if (snapErrMsg) log.warn('snapshot error surfaced to Analytics cards', { error: snapErrMsg });
  if (telErrMsg) log.warn('telemetry error surfaced to Analytics cards (partial data possible)', { error: telErrMsg });

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

  // Prefer backend task_count when present (cross-cutting uplift)
  const total = (snapshot as any)?.task_count ?? tasks.length;

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

  // Local mesh / hybrid (Recommendation 1): data-driven est + note when no Elastro savings docs
  const localMeshEst = tm?.local_mesh_estimated_savings ?? 0;
  const meshEfficiencyNote = tm?.mesh_efficiency_note ?? (tm as any)?.local_mesh_efficiency_note;
  
  // UX derivations for AST Savings card (local-est mode polish): clean value, conditional trend/partial/subtitle.
  // Preserves all original hybrid + LogLoom derivation logic and fields.
  const astSavingsValue = realSavings > 0 ? fmtTokens(realSavings) : (localMeshEst ? fmtTokens(localMeshEst) : "0");
  const astSavingsTrend = realSavings > 0
    ? { value: savingsPercent, label: `$${dollarsSaved.toFixed(2)} saved vs base cost`, suffix: '%' as const }
    : undefined;
  const astSavingsSubtitle = realSavings > 0 ? undefined : (localMeshEst ? "local estimate" : undefined);
  const astSavingsPartial = realSavings === 0 && localMeshEst === 0 && !snapLoading;
  const astSavingsSecondary = realSavings > 0 
    ? { label: 'Elastro', value: `${fmtTokens(baselineTokens)} baseline` } 
    : (meshEfficiencyNote ? { label: 'Mesh/LogLoom', value: 'routing-aware est.' } : undefined);

  const nodeLoads = (telemetry?.flume_node_load ?? []).map(l => ({
    name: l.tags['node_id'] || 'unknown',
    load: Math.round(l.value * 100)
  }));
  
  const routingDecisions = Object.entries((telemetry?.flume_routing_decision ?? []).reduce<Record<string, number>>((acc, d) => {
    const strategy = d.tags['strategy'] || 'unknown';
    acc[strategy] = (acc[strategy] || 0) + d.count;
    return acc;
  }, {})).map(([name, value]) => ({ name, value }));

  // Derived for Node Mesh card + partial detection (cross-cutting)
  const meshNodeCount = nodeLoads.length;
  const avgMeshLoad = meshNodeCount > 0 ? Math.round(nodeLoads.reduce((s, n) => s + n.load, 0) / meshNodeCount) : 0;
  const maxMeshLoad = meshNodeCount > 0 ? Math.max(...nodeLoads.map(n => n.load)) : 0;
  const meshHasData = meshNodeCount > 0;

  // Code Intelligence Backend (Rec 3): visibility into Elastro vs LogLoom AST structural indexing.
  // Data now read via normalized flat fields from useSystemState hook (promoted from telemetry in wire response).
  // Hook poll is slower (15s) since structural node counts change infrequently (on project ingest / logloom graph).
  // 0 is *valid complete information*, never treated as partial/empty.
  const systemState = useSystemState(15000);
  const codeIntelLoading = !systemState;
  const elasticAstCount: number = systemState?.elasticAstCount ?? 0;
  const logloomAstCount: number = systemState?.logloomAstCount ?? 0;
  const hasCodeIntel = elasticAstCount > 0 || logloomAstCount > 0;
  const structuralNodes = Math.max(elasticAstCount, logloomAstCount);
  const codeIntelStatus = elasticAstCount > 0 && logloomAstCount > 0 ? 'Hybrid' : elasticAstCount > 0 ? 'Elastro' : logloomAstCount > 0 ? 'LogLoom' : 'No AST indexes yet';

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Analytics</h1>
        <p className="text-sm text-muted-foreground mt-1">Performance metrics and intelligent swarm observability</p>
      </motion.div>

      {isInitialLoading && (
        <div className="flex items-center gap-2 text-muted-foreground py-10">
          <Loader2 className="w-4 h-4 animate-spin" /> Loading Live Analytics…
        </div>
      )}

      {!isInitialLoading && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-8 gap-4 relative z-10">
            <GlassMetricCard
              title="Total Tasks"
              value={String(total)}
              icon={Target}
              trend={{ value: done, label: `${done} done` }}
              helpText="Aggregate count of all tasks (epics / features / stories / tasks) tracked by the orchestrator snapshot. Prefers backend task_count for efficiency with legacy array fallback."
              loading={snapLoading}
              error={snapErrMsg}
              partial={!snapshot && !snapLoading}
            />
            <GlassMetricCard
              title="Review Pass Rate"
              value={`${passRate}%`}
              icon={TrendingUp}
              trend={{ value: passRate, label: `${approvedReviews}/${reviews.length} reviews` }}
              helpText="Automated review approval rate from the meta-critic pipeline. (approved verdicts / total reviews in snapshot). Indicates code quality signal before human handoff."
              loading={snapLoading}
              error={snapErrMsg}
              partial={reviews.length === 0 && !snapLoading}
            />
            <GlassMetricCard
              title="Active Workers"
              value={String(workers.length)}
              icon={Zap}
              trend={{ value: 0, label: `${workers.filter(w => w.status !== 'idle').length} busy` }}
              helpText="Total connected workers in the swarm from snapshot. Trend shows currently busy (non-idle) executors handling tasks."
              loading={snapLoading}
              error={snapErrMsg}
            />
            <GlassMetricCard
              title="Failure & Blocked"
              value={String(totalFailuresAndBlocked)}
              icon={Clock}
              trend={{ value: failures.length, label: `${failures.length} hard failures` }}
              helpText="Combined count of hard failures + currently blocked tasks requiring human intervention (e.g. AST structural issues)."
              loading={snapLoading}
              error={snapErrMsg}
              partial={failures.length === 0 && blocked === 0 && !snapLoading}
            />

            {/* Live Telemetry cards — now with full resilience + explanatory helpText (per Grok reviews) */}
            <GlassMetricCard
              title="Gateway Engines"
              value={String(telemetry?.flume_active_models?.length ?? 0)}
              icon={Activity}
              trend={{ value: telemetry?.flume_active_models?.length ?? 0, label: telemetry?.flume_active_models?.join(', ') || 'No models loaded' }}
              helpText="Currently loaded LLM models (active gauges) reported by the gateway from Ollama /api/ps probes + node registry. Drives ensemble routing."
              loading={telLoading}
              error={telErrMsg}
              partial={(telemetry && !telemetry.flume_active_models?.length) || false}
            />
            <GlassMetricCard
              title="System Memory"
              value={telemetry && typeof telemetry.go_memstats_sys_bytes === 'number' && !isNaN(telemetry.go_memstats_sys_bytes)
                ? `${Math.round(telemetry.go_memstats_sys_bytes / 1024 / 1024)}MB`
                : '—'}
              icon={Cpu}
              helpText="Go runtime total memory obtained from the OS (go_memstats_sys_bytes). Includes heap, stacks, and caches for the gateway + dashboard process. Falls back to local runtime when gateway live metrics unavailable."
              loading={telLoading}
              error={telErrMsg}
              secondary={telemetry && typeof telemetry.go_goroutines === 'number' ? { label: 'goroutines', value: telemetry.go_goroutines } : undefined}
            />
            <GlassMetricCard
              title="AST Savings"
              value={astSavingsValue}
              subtitle={astSavingsSubtitle}
              icon={TrendingUp}
              trend={astSavingsTrend}
              helpText="Hybrid model: Elastro (when present) delivers precise AST-aware compression savings vs naive full-context baseline (sourced from agent-token-telemetry 'savings' docs). In local-only / mesh mode (no Elastro data): data-driven estimate of tokens avoided via intelligent multi-node routing + LogLoom-path structural awareness, computed live from Telemetry Bridge (flume_worker_tokens_total + routing decisions + node loads). See mesh_efficiency_note for derivation details. In estimate mode the number is clean; 'local estimate' appears as subtitle and derivation is in secondary (no trend shown)."
              loading={snapLoading}
              error={snapErrMsg}
              partial={astSavingsPartial}
              secondary={astSavingsSecondary}
            />
            {/* New: Code Intelligence Backend card (Recommendation 3). Fixed empty/partial-at-0 rendering + normalized data path. */}
            <GlassMetricCard
              title="Code Intelligence Backend"
              value={codeIntelStatus}
              icon={Braces}
              helpText="Hybrid code-understanding visibility for agents. Elastro (flume-elastro-graph): semantic vector RAG over rich AST graphs for meaning-aware retrieval. LogLoom AST (flume-logloom-ast): precise structural nodes/call-graphs for exact 'walk the code' dependency traversal (avoids hallucinated boundaries). Structural nodes = max indexed across backends. Future hybrid (LogLoom surgical structure + Elastro semantic) will unlock superior AST Savings + context fidelity. Ingest of AST indexes happens automatically on `flume project clone` (or intake) for any repo. 0/0 is valid & complete: no projects have had AST graph ingested yet."
              loading={codeIntelLoading}
              error={null}
              secondary={{ label: 'Elastro / LogLoom nodes', value: `${elasticAstCount} / ${logloomAstCount}` }}
            />
            <GlassMetricCard
              title="VRAM Pressure"
              value={String(telemetry?.flume_vram_pressure_events_total ?? 0)}
              icon={ServerCrash}
              trend={{ value: telemetry?.flume_vram_pressure_events_total ?? 0, label: 'Ensemble clamps' }}
              helpText="Cumulative times complex ensemble requests were degraded due to live VRAM headroom from Ollama /api/ps + FLUME_SYSTEM_MEMORY_GB. See gateway/health_checker.go:probeLoad (sum size_vram / MemoryGB)."
              loading={telLoading}
              error={telErrMsg}
              partial={telemetry && telemetry.flume_vram_pressure_events_total === 0}
            />
            {/* Node Mesh summary card (covers "Node Mesh Distribution section" resilience + helpText) */}
            <GlassMetricCard
              title="Mesh Load (Avg/Max)"
              value={`${avgMeshLoad}%`}
              icon={Gauge}
              helpText="VRAM memory pressure = sum(loaded model size_vram from Ollama /api/ps) / node declared MemoryGB (auto-discovered via health probes in node_registry + health_checker). Per-node gauges exposed as flume_node_load."
              loading={telLoading}
              error={telErrMsg}
              partial={!meshHasData && !telLoading}
              secondary={{ label: 'nodes / max', value: `${meshNodeCount} / ${maxMeshLoad}%` }}
            />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-4 gap-5 relative z-10">
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1 }} className="glass-card p-5">
              <h3
                className="text-sm font-semibold text-foreground mb-4 flex items-center gap-1.5"
                title="Per-node VRAM memory pressure = sum loaded model size_vram / node declared MemoryGB. See the 'Mesh Load (Avg/Max)' card above for live values and full explanation."
              >
                Node Mesh Distribution
                <Info className="w-3.5 h-3.5 text-muted-foreground/60" aria-label="Mesh load formula help" />
              </h3>
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
