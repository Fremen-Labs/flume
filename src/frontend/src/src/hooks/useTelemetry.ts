import { useQuery } from '@tanstack/react-query';
import { safeFetchJson } from '@/utils/safeFetch';

export interface TelemetryData {
  go_goroutines: number;
  go_memstats_alloc_bytes: number;
  go_memstats_sys_bytes: number;
  flume_up: number;
  flume_escalation_total: number;
  flume_build_info: string;
  flume_vram_pressure_events_total: number;
  flume_concurrency_throttled_total: number;
  flume_tasks_blocked_total: number;
  flume_active_models: string[];
  flume_ensemble_requests_total: {
    tags: Record<string, string>;
    count: number;
  }[];
  flume_worker_tokens_total: {
    tags: Record<string, string>;
    count: number;
  }[];
  flume_node_requests_total: {
    tags: Record<string, string>;
    count: number;
  }[];
  flume_routing_decision: {
    tags: Record<string, string>;
    count: number;
  }[];
  flume_node_load: {
    tags: Record<string, string>;
    value: number;
  }[];
}

export function useTelemetry() {
  return useQuery<TelemetryData>({
    queryKey: ['telemetry'],
    queryFn: () => safeFetchJson<TelemetryData>('/api/telemetry'),
    refetchInterval: 5000,
  });
}

