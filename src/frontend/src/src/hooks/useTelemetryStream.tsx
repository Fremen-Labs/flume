import { useState, useEffect } from 'react';

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
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/telemetry`;
    let ws: WebSocket | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(wsUrl);

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload.event === 'telemetry') {
              const newLog = payload.data as TelemetryLog;
              setLogs(prev => [newLog, ...prev].slice(0, 100));
          }
        } catch (e) {
          console.error("Telemetry WebSocket parse error:", e);
        }
      };

      ws.onclose = () => {
        reconnectTimeout = setTimeout(connect, 3000);
      };
      
      ws.onerror = () => {
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
