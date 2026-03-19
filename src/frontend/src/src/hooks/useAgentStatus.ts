import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

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
    queryFn: () => fetch('/api/workflow/agents/status').then(r => r.json()),
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
      fetch('/api/workflow/agents/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.json()),
    onSuccess: invalidate,
  });

  const stop = useMutation({
    mutationFn: () =>
      fetch('/api/workflow/agents/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => r.json()),
    onSuccess: invalidate,
  });

  return { start, stop };
}
