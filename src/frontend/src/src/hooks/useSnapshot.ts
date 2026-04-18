import { useQuery } from '@tanstack/react-query';
import type { Snapshot } from '@/types';

async function fetchSnapshot(): Promise<Snapshot> {
  const res = await fetch('/api/snapshot');
  if (!res.ok) {
    let msg = `Snapshot fetch failed: ${res.status}`;
    try {
      const body = await res.json();
      if (body?.error) msg = body.error;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return res.json();
}

export function useSnapshot() {
  return useQuery<Snapshot>({
    queryKey: ['snapshot'],
    queryFn: fetchSnapshot,
    refetchInterval: (query) => (query.state.status === 'error' ? false : 5_000),
    refetchIntervalInBackground: false,
    staleTime: 3_000,
    retry: false,
  });
}
