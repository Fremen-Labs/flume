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
          let parsedData = payload;
          if (payload.event === 'update' && typeof payload.data === 'string') {
              try {
                  parsedData = JSON.parse(payload.data);
              } catch {
                  parsedData = { msg: payload.data };
              }
          }

          const time = new Date().toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
          const newLog: TelemetryLog = {
            id: parsedData.id || crypto.randomUUID(),
            msg: parsedData.msg || parsedData.message || JSON.stringify(parsedData),
            time: parsedData.time || time,
            level: parsedData.level || (parsedData.msg?.toLowerCase().includes('error') ? 'ERROR' : 'INFO')
          };
          
          setLogs(prev => {
            return [newLog, ...prev].slice(0, 100);
          });
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
