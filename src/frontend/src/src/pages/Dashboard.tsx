import React from 'react';
import { motion } from 'framer-motion';
import {
  Activity, Zap, Cpu, Server, Shield, Radio, RefreshCw, AlertTriangle, Terminal, HardDrive
} from 'lucide-react';
import { useTelemetry } from '@/hooks/useTelemetry';
import { useTelemetryStream } from '@/hooks/useTelemetryStream';
import { GlassMetricCard } from '@/components/GlassMetricCard';

function formatBytes(bytes?: number): string {
  if (!bytes) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

export default function Dashboard() {
  const { data: telemetry, isLoading, isError, refetch } = useTelemetry();
  const streamLogs = useTelemetryStream();

  const totalNodeRequests = (telemetry?.flume_node_requests_total || [])
    .reduce((sum, item) => sum + (item.count || 0), 0);

  const activeModelsCount = (telemetry?.flume_active_models || []).length;
  const memAlloc = formatBytes(telemetry?.go_memstats_alloc_bytes);
  const goroutines = telemetry?.go_goroutines || 0;
  const isUp = telemetry?.flume_up === 1;

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      {/* Header Bar */}
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-border/40 pb-5 relative z-10"
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-primary/15 flex items-center justify-center breathing">
            <Radio className="w-5 h-5 text-primary icon-glow-active" />
          </div>
          <div>
            <h1 className="text-xl font-bold tracking-tight text-foreground flex items-center gap-2">
              Gateway Telemetry Stream
            </h1>
            <p className="text-xs text-muted-foreground">
              Real-time Golang Gateway metrics & websocket event telemetric feed
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="glass-card px-3.5 py-1.5 flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className={`animate-ping absolute inline-flex h-full w-full rounded-full ${isUp ? 'bg-emerald-500' : 'bg-destructive'} opacity-75`} />
              <span className={`relative inline-flex rounded-full h-2 w-2 ${isUp ? 'bg-emerald-500' : 'bg-destructive'}`} />
            </span>
            <span className="text-foreground font-semibold">{isUp ? 'ONLINE' : 'OFFLINE'}</span>
            <span className="text-muted-foreground">Gateway Core</span>
          </div>

          <button
            onClick={() => refetch()}
            className="p-2 rounded-lg bg-card/60 border border-border/50 hover:bg-muted/80 text-muted-foreground hover:text-foreground transition-all"
            title="Refresh Telemetry"
          >
            <RefreshCw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </motion.div>

      {/* Top Metric Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
        <GlassMetricCard
          title="Total Gateway Requests"
          value={totalNodeRequests}
          icon={Zap}
          subtitle="Processed Requests"
        />
        <GlassMetricCard
          title="Active Goroutines"
          value={goroutines}
          icon={Cpu}
          subtitle="Go Runtime Threads"
        />
        <GlassMetricCard
          title="Heap Memory Allocated"
          value={memAlloc}
          icon={HardDrive}
          subtitle="Go MemStats Alloc"
        />
        <GlassMetricCard
          title="Concurrency Throttled"
          value={telemetry?.flume_concurrency_throttled_total || 0}
          icon={AlertTriangle}
          subtitle="Rate Limiter Holds"
        />
      </div>

      {/* Main Grid: Live Telemetric Log Feed + Model Metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column (2 cols): Real-Time WebSocket Telemetry Feed */}
        <div className="lg:col-span-2 border border-border bg-card/50 rounded-xl p-6 shadow-sm flex flex-col gap-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Terminal className="w-5 h-5 text-primary" />
              <h2 className="text-lg font-bold">Live Gateway Event Stream</h2>
            </div>
            <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-primary/10 text-primary border border-primary/20">
              /ws/telemetry
            </span>
          </div>

          <div className="bg-black/80 rounded-xl p-4 border border-white/10 font-mono text-xs h-[420px] overflow-y-auto space-y-2">
            {streamLogs.length === 0 ? (
              <div className="text-muted-foreground/60 italic p-4 text-center">
                Awaiting telemetry events from Golang gateway websocket...
              </div>
            ) : (
              streamLogs.map((log) => (
                <div key={log.id || Math.random()} className="flex items-start gap-2 py-1 border-b border-white/5 last:border-0">
                  <span className="text-muted-foreground text-[10px] whitespace-nowrap">
                    {new Date(log.time).toLocaleTimeString()}
                  </span>
                  <span className={`px-1.5 py-0.2 rounded text-[9px] uppercase font-bold ${
                    log.level === 'error' ? 'bg-destructive/20 text-destructive' :
                    log.level === 'warn' ? 'bg-amber-500/20 text-amber-400' : 'bg-emerald-500/20 text-emerald-400'
                  }`}>
                    {log.level || 'INFO'}
                  </span>
                  <span className="text-foreground/90 break-all">{log.msg}</span>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Right Column (1 col): Model & Routing Telemetry Breakdown */}
        <div className="border border-border bg-card/50 rounded-xl p-6 shadow-sm flex flex-col gap-5">
          <div className="flex items-center gap-2">
            <Server className="w-5 h-5 text-blue-400" />
            <h2 className="text-lg font-bold">Model & Node Mesh</h2>
          </div>

          {/* Active Models */}
          <div className="space-y-3">
            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Active Models ({activeModelsCount})
            </div>
            {(telemetry?.flume_active_models || []).length === 0 ? (
              <div className="text-xs text-muted-foreground italic">No active model tags registered.</div>
            ) : (
              <div className="flex flex-wrap gap-2">
                {(telemetry?.flume_active_models || []).map((m) => (
                  <span key={m} className="px-2.5 py-1 rounded-lg bg-primary/10 border border-primary/20 text-xs font-mono text-primary font-medium">
                    {m}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Node Requests Breakdown */}
          <div className="space-y-3">
            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Node Requests Distribution
            </div>
            {(telemetry?.flume_node_requests_total || []).length === 0 ? (
              <div className="text-xs text-muted-foreground italic">No node request metrics recorded yet.</div>
            ) : (
              <div className="space-y-2">
                {(telemetry?.flume_node_requests_total || []).map((nr, idx) => (
                  <div key={idx} className="flex items-center justify-between p-2.5 rounded-lg bg-background/40 border border-border/40 text-xs font-mono">
                    <span className="text-foreground font-medium truncate max-w-[180px]">
                      {nr.tags?.node || nr.tags?.model || 'default'}
                    </span>
                    <span className="text-primary font-bold">{nr.count} reqs</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

