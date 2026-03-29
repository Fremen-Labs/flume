import { useState, useEffect } from 'react';

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
}

export function useSystemState(pollInterval: number = 2000) {
  const [data, setData] = useState<SystemState | null>(null);

  useEffect(() => {
    const fetchState = async () => {
      try {
        const res = await fetch('/api/system-state');
        if (res.ok) {
          const json = await res.json();
          setData(json);
        }
      } catch (e) {
        console.error('Failed to fetch system state', e);
      }
    };
    
    fetchState();
    const interval = setInterval(fetchState, pollInterval);
    return () => clearInterval(interval);
  }, [pollInterval]);

  return data;
}
