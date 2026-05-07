import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { safeFetchJson } from '@/utils/safeFetch';

export interface AgentStatus {
  running: boolean;
  manager_running: boolean;
  handlers_running: boolean;
  manager_pids: number[];
  handler_pids: number[];
}

export function useAgentStatus() {
  return useQuery<AgentStatus>({
    queryKey: ['agent-status'],
    queryFn: () => safeFetchJson<AgentStatus>('/api/workflow/agents/status'),
    refetchInterval: 5_000,
    staleTime: 3_000,
  });
}

export function useAgentControls() {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['agent-status'] });
    qc.invalidateQueries({ queryKey: ['snapshot'] });
  };

  const start = useMutation({
    mutationFn: () =>
      safeFetchJson('/api/workflow/agents/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
    onSuccess: invalidate,
  });

  const stop = useMutation({
    mutationFn: () =>
      safeFetchJson('/api/workflow/agents/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
    onSuccess: invalidate,
  });

  return { start, stop };
}

