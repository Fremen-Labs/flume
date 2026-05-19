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
}

export function useSystemState(pollInterval: number = 2000) {
  const [data, setData] = useState<SystemState | null>(null);

  useEffect(() => {
    const fetchState = async () => {
      try {
        const json = await safeFetchJson<SystemState>('/api/system-state');
        setData(json);
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

