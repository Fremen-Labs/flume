import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Server, Plus, Trash2, RefreshCw, CheckCircle2, AlertTriangle,
  XCircle, Cpu, MemoryStick, Gauge, Clock, Zap, ChevronDown, ChevronUp, Wifi,
} from 'lucide-react';
import { GlassMetricCard } from '@/components/GlassMetricCard';

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

interface NodeCapabilities {
  reasoning_score: number;  // 1-10
  max_context: number;
  quantization: string;
  estimated_tps: number;
  memory_gb: number;
}

interface NodeHealth {
  status: 'healthy' | 'degraded' | 'offline' | '';
  last_seen: string;
  current_load: number;
  loaded_models: string[];
  latency_ms: number;
}

interface OllamaNode {
  id: string;
  host: string;
  model_tag: string;
  capabilities: NodeCapabilities;
  health: NodeHealth;
}

interface NodesResponse {
  nodes: OllamaNode[];
  count: number;
}

interface TestResult {
  node_id: string;
  host: string;
  reachable: boolean;
  latency_ms: number;
  models: string[];
  current_load: number;
  error: string | null;
}

interface AddNodeForm {
  id: string;
  host: string;
  model_tag: string;
  capabilities: Partial<NodeCapabilities>;
}

// ─────────────────────────────────────────────────────────────────────────────
// API helpers
// ─────────────────────────────────────────────────────────────────────────────

async function fetchNodes(): Promise<NodesResponse> {
  const res = await fetch('/api/nodes');
  if (!res.ok) throw new Error(`GET /api/nodes → ${res.status}`);
  return res.json();
}

async function addNode(payload: AddNodeForm): Promise<void> {
  const res = await fetch('/api/nodes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error ?? `POST /api/nodes → ${res.status}`);
  }
}

async function deleteNode(nodeId: string): Promise<void> {
  const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error ?? `DELETE /api/nodes → ${res.status}`);
  }
}

async function testNode(nodeId: string): Promise<TestResult> {
  const res = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/test`, { method: 'POST' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error ?? `POST /api/nodes/${nodeId}/test → ${res.status}`);
  }
  return res.json();
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

const STATUS_CONFIG = {
  healthy:  { icon: CheckCircle2, color: 'text-emerald-400', bg: 'bg-emerald-400/10', label: 'Healthy' },
  degraded: { icon: AlertTriangle, color: 'text-amber-400',  bg: 'bg-amber-400/10',  label: 'Degraded' },
  offline:  { icon: XCircle,       color: 'text-red-400',    bg: 'bg-red-400/10',    label: 'Offline' },
  '':       { icon: XCircle,       color: 'text-slate-400',  bg: 'bg-slate-400/10',  label: 'Unknown' },
} as const;

function StatusBadge({ status }: { status: string }) {
  const cfg = STATUS_CONFIG[status as keyof typeof STATUS_CONFIG] ?? STATUS_CONFIG[''];
  const Icon = cfg.icon;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${cfg.color} ${cfg.bg}`}>
      <Icon className="w-3 h-3" />
      {cfg.label}
    </span>
  );
}

function LoadBar({ load }: { load: number }) {
  const pct = Math.min(100, Math.round((load ?? 0) * 100));
  const color = pct >= 80 ? 'bg-red-500' : pct >= 55 ? 'bg-amber-500' : 'bg-emerald-500';
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 rounded-full bg-white/8 overflow-hidden">
        <motion.div
          className={`h-full rounded-full ${color}`}
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
        />
      </div>
      <span className="text-[10px] text-muted-foreground w-7 text-right">{pct}%</span>
    </div>
  );
}

function NodeCard({ node, onDelete }: { node: OllamaNode; onDelete: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const status = node.health?.status ?? '';
  const lastSeen = node.health?.last_seen
    ? new Date(node.health.last_seen).toLocaleTimeString()
    : '—';

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testNode(node.id);
      setTestResult(result);
    } catch {
      setTestResult({ node_id: node.id, host: node.host, reachable: false, latency_ms: 0, models: [], current_load: 0, error: 'Connection test failed' });
    } finally {
      setTesting(false);
    }
  };

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.96 }}
      className="glass-card p-4 flex flex-col gap-3"
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-indigo-500/15 flex items-center justify-center flex-shrink-0">
            <Server className="w-4 h-4 text-indigo-400" />
          </div>
          <div className="min-w-0">
            <p className="font-semibold text-sm text-foreground truncate">{node.id}</p>
            <p className="text-xs text-muted-foreground font-mono truncate">{node.host}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <StatusBadge status={status} />
          <button
            id={`node-test-${node.id}`}
            onClick={handleTest}
            disabled={testing}
            className="w-7 h-7 rounded-md flex items-center justify-center text-muted-foreground hover:text-cyan-400 hover:bg-cyan-400/10 transition-colors disabled:opacity-50"
            title="Test connection"
          >
            <Wifi className={`w-3.5 h-3.5 ${testing ? 'animate-pulse' : ''}`} />
          </button>
          <button
            id={`node-delete-${node.id}`}
            onClick={() => onDelete(node.id)}
            className="w-7 h-7 rounded-md flex items-center justify-center text-muted-foreground hover:text-red-400 hover:bg-red-400/10 transition-colors"
            title="Remove node"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Test result banner */}
      {testResult && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
          className={`rounded-lg px-3 py-2 text-xs border ${
            testResult.reachable
              ? 'bg-emerald-400/10 border-emerald-400/20 text-emerald-400'
              : 'bg-red-400/10 border-red-400/20 text-red-400'
          }`}
        >
          <div className="flex items-center gap-2 mb-1">
            {testResult.reachable ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
            <span className="font-medium">{testResult.reachable ? `Reachable — ${testResult.latency_ms}ms` : 'Unreachable'}</span>
          </div>
          {testResult.reachable && testResult.models.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {testResult.models.map(m => (
                <span key={m} className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/15 text-emerald-300 font-mono">{m}</span>
              ))}
            </div>
          )}
          {testResult.error && <p className="mt-1 text-[10px]">{testResult.error}</p>}
        </motion.div>
      )}

      {/* Model tag */}
      <div className="flex items-center gap-2">
        <Cpu className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
        <span className="text-xs text-foreground/80 font-mono">{node.model_tag || '—'}</span>
      </div>

      {/* Load bar */}
      <div>
        <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">Load</p>
        <LoadBar load={node.health?.current_load ?? 0} />
      </div>

      {/* Quick stats */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="bg-white/4 rounded-lg py-1.5">
          <p className="text-[10px] text-muted-foreground">Latency</p>
          <p className="text-xs font-semibold text-foreground">{node.health?.latency_ms ?? 0}ms</p>
        </div>
        <div className="bg-white/4 rounded-lg py-1.5">
          <p className="text-[10px] text-muted-foreground">Memory</p>
          <p className="text-xs font-semibold text-foreground">{node.capabilities?.memory_gb ?? 0}GB</p>
        </div>
        <div className="bg-white/4 rounded-lg py-1.5">
          <p className="text-[10px] text-muted-foreground">TPS</p>
          <p className="text-xs font-semibold text-foreground">{node.capabilities?.estimated_tps ?? 0}</p>
        </div>
      </div>

      {/* Expand/collapse for details */}
      <button
        id={`node-expand-${node.id}`}
        onClick={() => setExpanded(v => !v)}
        className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        {expanded ? 'Less' : 'Details'}
      </button>

      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="border-t border-border/20 pt-3 space-y-2 text-xs">
              <Row label="Reasoning Score" value={`${node.capabilities?.reasoning_score ?? 0}/10`} />
              <Row label="Max Context" value={(node.capabilities?.max_context ?? 0).toLocaleString()} />
              <Row label="Quantization" value={node.capabilities?.quantization ?? '—'} />
              <Row label="Last Seen" value={lastSeen} />
              {(node.health?.loaded_models?.length ?? 0) > 0 && (
                <div>
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">Loaded Models</p>
                  <div className="flex flex-wrap gap-1">
                    {node.health.loaded_models.map(m => (
                      <span key={m} className="px-1.5 py-0.5 rounded text-[10px] bg-indigo-500/15 text-indigo-300 font-mono">{m}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between items-center">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground font-mono">{value}</span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Add Node Form
// ─────────────────────────────────────────────────────────────────────────────

const BLANK_FORM: AddNodeForm = {
  id: '',
  host: '',
  model_tag: '',
  capabilities: { reasoning_score: 5, max_context: 32768, memory_gb: 16 },
};

function AddNodeModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<AddNodeForm>(BLANK_FORM);
  const [error, setError] = useState('');
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: addNode,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['nodes'] });
      onSaved();
    },
    onError: (e: Error) => setError(e.message),
  });

  const setField = (key: keyof AddNodeForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm(f => ({ ...f, [key]: e.target.value }));

  const setCap = (key: keyof NodeCapabilities) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm(f => ({ ...f, capabilities: { ...f.capabilities, [key]: parseFloat(e.target.value) || e.target.value } }));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="glass-card w-full max-w-md mx-4 p-6 space-y-5"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold text-foreground">Register Node</h2>
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">✕</button>
        </div>

        {error && (
          <div className="text-xs text-red-400 bg-red-400/10 border border-red-400/20 rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        <div className="space-y-3">
          <Field id="node-id" label="Node ID" placeholder="mac-mini-1" value={form.id} onChange={setField('id')} hint="Lowercase letters, numbers, and hyphens only" />
          <Field id="node-host" label="Host:Port" placeholder="192.168.1.50:11434" value={form.host} onChange={setField('host')} hint="Accessible hostname/IP with Ollama port" />
          <Field id="node-model" label="Primary Model Tag" placeholder="qwen2.5-coder:32b" value={form.model_tag} onChange={setField('model_tag')} />
          <div className="grid grid-cols-2 gap-3">
            <Field id="node-memory" label="Memory (GB)" type="number" value={String(form.capabilities.memory_gb ?? '')} onChange={setCap('memory_gb')} />
            <Field id="node-score" label="Reasoning Score (1-10)" type="number" value={String(form.capabilities.reasoning_score ?? '')} onChange={setCap('reasoning_score')} />
          </div>
        </div>

        <div className="flex gap-3 pt-2">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 rounded-lg text-sm text-muted-foreground border border-border/30 hover:bg-white/5 transition-colors"
          >
            Cancel
          </button>
          <button
            id="node-register-submit"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate(form)}
            className="flex-1 px-4 py-2 rounded-lg text-sm font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-60"
          >
            {mutation.isPending ? 'Registering…' : 'Register Node'}
          </button>
        </div>
      </motion.div>
    </div>
  );
}

function Field({
  id, label, placeholder = '', value, onChange, type = 'text', hint,
}: {
  id: string; label: string; placeholder?: string; value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  type?: string; hint?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-[11px] text-muted-foreground mb-1">{label}</label>
      <input
        id={id} type={type} placeholder={placeholder} value={value} onChange={onChange}
        autoComplete="off"
        className="w-full bg-white/5 border border-border/30 rounded-lg px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:ring-1 focus:ring-indigo-500/50"
      />
      {hint && <p className="text-[10px] text-muted-foreground/60 mt-0.5">{hint}</p>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

export default function NodesOverview() {
  const [showAdd, setShowAdd] = useState(false);
  const qc = useQueryClient();

  const { data, isLoading, isError, dataUpdatedAt } = useQuery<NodesResponse>({
    queryKey: ['nodes'],
    queryFn: fetchNodes,
    refetchInterval: 15_000,   // align with health checker probe interval
    staleTime: 10_000,
  });

  const deleteMut = useMutation({
    mutationFn: deleteNode,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['nodes'] }),
  });

  const nodes = data?.nodes ?? [];
  const healthy = nodes.filter(n => n.health?.status === 'healthy').length;
  const degraded = nodes.filter(n => n.health?.status === 'degraded').length;
  const offline  = nodes.filter(n => n.health?.status === 'offline').length;
  const avgLoad  = nodes.length
    ? Math.round(nodes.reduce((s, n) => s + (n.health?.current_load ?? 0), 0) / nodes.length * 100)
    : 0;
  const lastRefreshed = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : '—';

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">Node Mesh</h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            Distributed Ollama nodes · Auto-refreshes every 15s · Last updated {lastRefreshed}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            id="nodes-refresh"
            onClick={() => qc.invalidateQueries({ queryKey: ['nodes'] })}
            disabled={isLoading}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm text-muted-foreground border border-border/30 hover:bg-white/5 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
          <button
            id="nodes-add"
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Node
          </button>
        </div>
      </motion.div>

      {/* Summary metrics */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.05 }}
        className="grid grid-cols-2 sm:grid-cols-4 gap-4"
      >
        <GlassMetricCard title="Total Nodes" value={String(nodes.length)} icon={Server} trend={{ value: healthy, label: `${healthy} healthy` }} />
        <GlassMetricCard title="Healthy" value={String(healthy)} icon={CheckCircle2} trend={{ value: healthy, label: 'online & passing probes' }} />
        <GlassMetricCard title="Degraded / Offline" value={String(degraded + offline)} icon={AlertTriangle} trend={{ value: degraded, label: `${degraded} degraded, ${offline} offline` }} />
        <GlassMetricCard title="Avg Load" value={`${avgLoad}%`} icon={Gauge} trend={{ value: avgLoad, label: 'across healthy nodes' }} />
      </motion.div>

      {/* Empty state */}
      {!isLoading && !isError && nodes.length === 0 && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="glass-card p-12 flex flex-col items-center text-center gap-4"
        >
          <div className="w-16 h-16 rounded-2xl bg-indigo-500/10 flex items-center justify-center">
            <Server className="w-8 h-8 text-indigo-400" />
          </div>
          <div>
            <p className="font-semibold text-foreground">No nodes registered</p>
            <p className="text-sm text-muted-foreground mt-1">
              Register an Ollama node to enable distributed ensemble routing.
            </p>
          </div>
          <button
            id="nodes-add-empty"
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
          >
            <Plus className="w-4 h-4" />
            Register First Node
          </button>
        </motion.div>
      )}

      {/* Error state */}
      {isError && (
        <div className="glass-card p-6 flex items-center gap-3 text-sm text-red-400 border border-red-400/20">
          <XCircle className="w-4 h-4 flex-shrink-0" />
          Unable to reach the Gateway. Make sure it is running and accessible.
        </div>
      )}

      {/* Node grid */}
      {nodes.length > 0 && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.1 }}
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4"
        >
          <AnimatePresence>
            {nodes.map(node => (
              <NodeCard
                key={node.id}
                node={node}
                onDelete={(id) => {
                  if (window.confirm(`Remove node "${id}" from the mesh?`)) {
                    deleteMut.mutate(id);
                  }
                }}
              />
            ))}
          </AnimatePresence>
        </motion.div>
      )}

      {/* Add node modal */}
      <AnimatePresence>
        {showAdd && (
          <AddNodeModal
            onClose={() => setShowAdd(false)}
            onSaved={() => setShowAdd(false)}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
