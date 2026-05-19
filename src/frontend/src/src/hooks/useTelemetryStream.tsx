import { useState, useEffect } from 'react';
import { createLogger } from '@/utils/logger';

const log = createLogger('hooks.useTelemetryStream');

export interface TelemetryLog {
  id: string;
  msg: string;
  time: string;
  level: string;
}

export function useTelemetryStream() {
  const [logs, setLogs] = useState<TelemetryLog[]>([]);

  useEffect(() => {
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Use the actual backend orchestrator port (typically 8765) if in dev or use current host if proxied
    let wsUrl = `${wsProtocol}//${window.location.host}/ws/telemetry`;
    const token = localStorage.getItem('flume-admin-token');
    if (token) {
      wsUrl += `?token=${encodeURIComponent(token)}`;
    }
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(wsUrl);
      log.debug('connect', 'WebSocket connecting', { url: wsUrl });

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.event === 'telemetry') {
              const newLog = payload.data as TelemetryLog;
              setLogs(prev => [newLog, ...prev].slice(0, 100));
          }
        } catch (e) {
          log.error('onmessage', 'WebSocket parse error', { error: String(e) });
        }
      };

      ws.onclose = (ev) => {
        log.warn('onclose', `WebSocket closed (code ${ev.code}), reconnecting in 3s`);
        reconnectTimeout = setTimeout(connect, 3000);
      };

      ws.onerror = () => {
        log.warn('onerror', 'WebSocket error — closing connection');
        ws?.close();
      };
    };

    connect();

    return () => {
      clearTimeout(reconnectTimeout);
      if (ws) {
        ws.close();
      }
    };
  }, []);

  return logs;
}

