import { useQuery } from '@tanstack/react-query';

export interface TelemetryData {
  go_goroutines: number;
  go_memstats_alloc_bytes: number;
  go_memstats_sys_bytes: number;
  flume_up: number;
  flume_escalation_total: number;
  flume_build_info: string;
  flume_vram_pressure_events_total: number;
  flume_active_models: string[];
  flume_ensemble_requests_total: {
    tags: Record<string, string>;
    count: number;
  }[];
}

export function useTelemetry() {
  return useQuery<TelemetryData>({
    queryKey: ['telemetry'],
    queryFn: async () => {
      const res = await fetch('/api/telemetry');
      if (!res.ok) {
        throw new Error('Failed to fetch telemetry');
      }
      return res.json();
    },
    refetchInterval: 5000,
  });
}
