import { useState, useEffect } from 'react';
import type { SystemState } from './useSystemState';

export function useWorkerHistory(systemState: SystemState | null) {
  const [history, setHistory] = useState<{ name: string; active: number; idle: number }[]>([]);

  useEffect(() => {
    if (!systemState) return;

    const activeCount = systemState.workers.filter(w => w.status === 'claimed' || w.status === 'active').length;
    const idleCount = systemState.workers.filter(w => w.status === 'idle').length;
          
    setHistory(prev => {
      const now = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const arr = [...prev, { name: now, active: activeCount, idle: idleCount }];
      return arr.length > 25 ? arr.slice(arr.length - 25) : arr;
    });
  }, [systemState]);

  return history;
}
