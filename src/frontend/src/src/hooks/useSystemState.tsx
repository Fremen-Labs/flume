import { useState, useEffect } from 'react';
import { safeFetchJson } from '@/utils/safeFetch';
import { createLogger } from '@/utils/logger';

const log = createLogger('hooks.useSystemState');

export interface WorkerState {
  name: string;
  role: string;
  model: string;
  execution_host: string;
  llm_provider: string;
  status: string;
  current_task_title?: string;
  heartbeat_at: string;
}

export interface SystemState {
  updated_at: string;
  workers: WorkerState[];
  telemetry?: Record<string, any>;
  // Normalized data path (clean contract): AST counts promoted to top-level for direct access.
  // Source of truth on wire is inside telemetry (or root for future backend evolution).
  // This fixes fragile (as any).telemetry reads and makes 0/0 a first-class observable value.
  elasticAstCount?: number;
  logloomAstCount?: number;
}

export function useSystemState(pollInterval: number = 2000) {
  const [data, setData] = useState<SystemState | null>(null);

  useEffect(() => {
    const fetchState = async () => {
      try {
        const json = await safeFetchJson<SystemState>('/api/system-state');
        // Data-path normalization (the fix for contract robustness + empty card root cause):
        // Promote counts to flat SystemState root so readers (AnalyticsPage etc) don't have to
        // know internal telemetry nesting. Handles both current (telemetry) and future (root) backend shapes.
        const normalized: SystemState = {
          ...json,
          elasticAstCount: (json as any)?.elasticAstCount ?? (json?.telemetry as any)?.elasticAstCount ?? 0,
          logloomAstCount: (json as any)?.logloomAstCount ?? (json?.telemetry as any)?.logloomAstCount ?? 0,
        };
        setData(normalized);
      } catch (e) {
        log.error('fetchState', 'Failed to fetch system state', { error: String(e) });
      }
    };
    
    fetchState();
    const interval = setInterval(fetchState, pollInterval);
    return () => clearInterval(interval);
  }, [pollInterval]);

  return data;
}

