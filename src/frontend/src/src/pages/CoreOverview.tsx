import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { Activity, Database, KeyRound, Radio, Server } from 'lucide-react';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { StatusBadge } from '@/components/StatusBadge';
import { createLogger } from '@/utils/logger';

const log = createLogger('pages.CoreOverview');

export interface StackDep {
  status: string;
  error?: string;
}

export interface StackStatus {
  service: string;
  gateway: StackDep;
  elasticsearch: StackDep;
  openbao: StackDep;
  nodes: number;
}

async function fetchStack(): Promise<StackStatus> {
  const res = await fetch('/api/stack');
  if (!res.ok) {
    log.error('fetchStack', `GET /api/stack → ${res.status}`);
    throw new Error(`Stack status failed: ${res.status}`);
  }
  return res.json();
}

function depBadge(status?: string): string {
  switch ((status || '').toLowerCase()) {
    case 'ok':
    case 'healthy':
      return 'healthy';
    case 'skipped':
    case 'disabled':
      return 'idle';
    case 'sealed':
    case 'uninitialized':
      return 'waiting';
    default:
      return 'failed';
  }
}

export default function CoreOverview() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['stack'],
    queryFn: fetchStack,
    refetchInterval: 5_000,
  });

  return (
    <div className="p-5 lg:p-6 max-w-[1400px] mx-auto space-y-6">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-semibold tracking-tight">Core stack</h1>
        <p className="text-sm text-muted-foreground mt-1">
          OpenBao, Elasticsearch, Gateway, and this console. Workers and the full agent mesh are not part of this stack.
        </p>
      </motion.div>

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
        <GlassMetricCard
          title="Gateway"
          value={data?.gateway?.status ?? (isLoading ? '…' : '—')}
          icon={Radio}
          loading={isLoading}
          error={error ? String(error) : data?.gateway?.error}
          helpText="LLM router on :8090. This console proxies /api and /v1 to it."
          glow={data?.gateway?.status === 'ok'}
        />
        <GlassMetricCard
          title="Elasticsearch"
          value={data?.elasticsearch?.status ?? (isLoading ? '…' : '—')}
          icon={Database}
          loading={isLoading}
          error={data?.elasticsearch?.error}
          helpText="State store for node registry, routing policy, and LLM config."
        />
        <GlassMetricCard
          title="OpenBao"
          value={data?.openbao?.status ?? (isLoading ? '…' : '—')}
          icon={KeyRound}
          loading={isLoading}
          error={data?.openbao?.error}
          helpText="Secret backend for LLM credentials. Dev token is injected at compose time."
        />
        <GlassMetricCard
          title="Node mesh"
          value={data?.nodes ?? 0}
          icon={Server}
          loading={isLoading}
          helpText="Registered local/frontier inference nodes. Add them on the Nodes page."
          subtitle="registered"
        />
      </div>

      <div className="glass-card p-5 space-y-3">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-primary" />
          <h2 className="text-sm font-semibold">Dependencies</h2>
        </div>
        <ul className="space-y-2 text-sm">
          {[
            { name: 'Gateway', dep: data?.gateway },
            { name: 'Elasticsearch', dep: data?.elasticsearch },
            { name: 'OpenBao', dep: data?.openbao },
          ].map((row) => (
            <li key={row.name} className="flex items-center justify-between gap-3">
              <span className="text-muted-foreground">{row.name}</span>
              <div className="flex items-center gap-2">
                {row.dep?.error && (
                  <span className="text-xs text-destructive max-w-[28rem] truncate" title={row.dep.error}>
                    {row.dep.error}
                  </span>
                )}
                <StatusBadge status={depBadge(row.dep?.status)} />
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
