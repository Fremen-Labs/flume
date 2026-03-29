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

export function useSystemState() {
  const [data, setData] = useState<SystemState | null>(null);
  const [history, setHistory] = useState<{ name: string, active: number, idle: number }[]>([]);
  const [logs, setLogs] = useState<{ id: string, msg: string, time: string, level: string }[]>([]);

  useEffect(() => {
    const fetchState = async () => {
      try {
        const res = await fetch('/api/system-state');
        if (res.ok) {
          const json = await res.json();
          setData(json);
          
          const activeCount = json.workers.filter((w: any) => w.status === 'claimed' || w.status === 'active').length;
          const idleCount = json.workers.filter((w: any) => w.status === 'idle').length;
          
          setHistory(prev => {
             const now = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second:'2-digit' });
             const arr = [...prev, { name: now, active: activeCount, idle: idleCount }];
             return arr.length > 25 ? arr.slice(arr.length - 25) : arr;
          });

          try {
            const logsRes = await fetch('/api/logs');
            if (logsRes.ok) {
              const logsJson = await logsRes.json();
              setLogs(prev => {
                 if (logsJson.length === 0) return prev;
                 const combined = [...logsJson, ...prev];
                 
                 const uniqueMap = new Map();
                 for (const log of combined) {
                   if (!uniqueMap.has(log.id)) {
                     uniqueMap.set(log.id, log);
                   }
                 }
                 const distinct = Array.from(uniqueMap.values());
                 return distinct.length > 60 ? distinct.slice(0, 60) : distinct;
              });
            }
          } catch(e) {
            console.error('Failed to fetch telemetry streams');
          }
        }
      } catch (e) {
        console.error('Failed to fetch system state', e);
      }
    };
    
    fetchState();
    const interval = setInterval(fetchState, 2000);
    return () => clearInterval(interval);
  }, []);

  return { data, history, logs, isLoading: !data };
}
