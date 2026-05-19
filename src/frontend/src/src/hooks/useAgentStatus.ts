import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { safeFetchJson } from '@/utils/safeFetch';
import { createLogger } from '@/utils/logger';

const log = createLogger('hooks.useAgentStatus');

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
    onSuccess: () => {
      log.info('useAgentControls', 'Agent workers started');
      invalidate();
    },
    onError: (err) => {
      log.error('useAgentControls', 'Failed to start agent workers', { error: String(err) });
    },
  });

  const stop = useMutation({
    mutationFn: () =>
      safeFetchJson('/api/workflow/agents/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
    onSuccess: () => {
      log.info('useAgentControls', 'Agent workers stopped');
      invalidate();
    },
    onError: (err) => {
      log.error('useAgentControls', 'Failed to stop agent workers', { error: String(err) });
    },
  });

  return { start, stop };
}


