import { useQuery } from '@tanstack/react-query';
import type { Snapshot } from '@/types';

async function fetchSnapshot(): Promise<Snapshot> {
  const res = await fetch('/api/snapshot');
  if (!res.ok) throw new Error(`Snapshot fetch failed: ${res.status}`);
  return res.json();
}

export function useSnapshot() {
  return useQuery<Snapshot>({
    queryKey: ['snapshot'],
    queryFn: fetchSnapshot,
    refetchInterval: 5_000,
    staleTime: 3_000,
  });
}
