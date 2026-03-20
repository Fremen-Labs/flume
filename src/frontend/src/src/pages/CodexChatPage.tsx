import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import {
  Loader2,
  MessageSquareCode,
  Plug,
  PlugZap,
  Send,
  ShieldAlert,
  Trash2,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import type { CodexAppServerProxyConfigResponse } from '@/types';

async function fetchProxyConfig(): Promise<CodexAppServerProxyConfigResponse> {
  const res = await fetch('/api/codex-app-server/proxy-config');
  if (!res.ok) throw new Error(`proxy-config failed: ${res.status}`);
  return res.json();
}

type LogLine = { dir: 'in' | 'out' | 'sys'; text: string; t: number };

function isJsonRpcServerRequest(obj: unknown): obj is { jsonrpc: string; method: string; id: string | number; params?: unknown } {
  if (!obj || typeof obj !== 'object') return false;
  const o = obj as Record<string, unknown>;
  return (
    o.jsonrpc === '2.0' &&
    typeof o.method === 'string' &&
    'id' in o &&
    o.id !== null &&
    o.id !== undefined
  );
}

export default function CodexChatPage() {
  const { data: cfg, isLoading, error, refetch } = useQuery({
    queryKey: ['codex', 'proxy-config'],
    queryFn: fetchProxyConfig,
    refetchInterval: 30_000,
  });

  const [log, setLog] = useState<LogLine[]>([]);
  const [outgoing, setOutgoing] = useState('');
  const [wsState, setWsState] = useState<'idle' | 'connecting' | 'open' | 'closed'>('idle');
  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pending, setPending] = useState<
    Record<string, { jsonrpc: string; method: string; id: string | number; params?: unknown }>
  >({});

  const appendLog = useCallback((dir: LogLine['dir'], text: string) => {
    setLog((prev) => [...prev.slice(-400), { dir, text, t: Date.now() }]);
  }, []);

  const disconnect = useCallback(() => {
    wsRef.current?.close();
    wsRef.current = null;
    setWsState('closed');
  }, []);

  const connect = useCallback(() => {
    if (!cfg?.clientWsUrl || !cfg.proxyRunning) return;
    disconnect();
    setWsState('connecting');
    appendLog('sys', `Connecting to ${cfg.clientWsUrl} …`);
    const ws = new WebSocket(cfg.clientWsUrl);
    wsRef.current = ws;
    ws.onopen = () => {
      setWsState('open');
      appendLog('sys', 'WebSocket open (relayed to Codex app-server).');
    };
    ws.onclose = (ev) => {
      setWsState('closed');
      appendLog('sys', `WebSocket closed (code ${ev.code}).`);
      wsRef.current = null;
    };
    ws.onerror = () => {
      appendLog('sys', 'WebSocket error (see browser devtools / network).');
    };
    ws.onmessage = (ev) => {
      const raw = typeof ev.data === 'string' ? ev.data : '(binary frame)';
      appendLog('in', raw);
      try {
        const obj = JSON.parse(raw) as unknown;
        if (isJsonRpcServerRequest(obj)) {
          const key = String(obj.id);
          setPending((p) => ({ ...p, [key]: obj }));
        }
      } catch {
        /* not JSON */
      }
    };
  }, [cfg?.clientWsUrl, cfg?.proxyRunning, appendLog, disconnect]);

  useEffect(() => () => disconnect(), [disconnect]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  const sendJson = useCallback(
    (raw: string) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        appendLog('sys', 'Not connected — connect first.');
        return;
      }
      const trimmed = raw.trim();
      if (!trimmed) return;
      ws.send(trimmed);
      appendLog('out', trimmed);
    },
    [appendLog],
  );

  const reply = useCallback(
    (id: string | number, body: Record<string, unknown>) => {
      sendJson(JSON.stringify({ jsonrpc: '2.0', id, ...body }));
      setPending((p) => {
        const next = { ...p };
        delete next[String(id)];
        return next;
      });
    },
    [sendJson],
  );

  const pendingList = useMemo(() => Object.values(pending), [pending]);

  const canConnect = Boolean(cfg?.proxyRunning && cfg?.websocketsInstalled);

  return (
    <div className="p-5 lg:p-6 max-w-5xl mx-auto space-y-6 relative">
      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4"
      >
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-primary/15 flex items-center justify-center">
            <MessageSquareCode className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-foreground">Codex</h1>
            <p className="text-xs text-muted-foreground">
              JSON-RPC over WebSocket — same protocol as{' '}
              <code className="text-[10px]">codex app-server</code>
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading}>
            {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Refresh config
          </Button>
          {canConnect && wsState !== 'open' ? (
            <Button
              type="button"
              size="sm"
              onClick={connect}
              disabled={wsState === 'connecting' || isLoading}
            >
              <Plug className="h-4 w-4 mr-1" />
              Connect
            </Button>
          ) : null}
          {wsState === 'open' ? (
            <Button type="button" variant="secondary" size="sm" onClick={disconnect}>
              <PlugZap className="h-4 w-4 mr-1" />
              Disconnect
            </Button>
          ) : null}
        </div>
      </motion.div>

      <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 flex gap-3 text-sm">
        <ShieldAlert className="h-5 w-5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
        <div className="space-y-1 text-muted-foreground">
          <p>
            The proxy exposes your <strong className="text-foreground">Codex session</strong> to anyone who can reach{' '}
            <code className="text-xs">ws://&lt;host&gt;:{cfg?.proxyPort ?? '…'}/</code>. Use{' '}
            <code className="text-xs">FLUME_CODEX_WS_PROXY_BIND=127.0.0.1</code> on shared machines.
          </p>
          <p className="text-xs">
            Run <code className="text-[10px]">./flume codex-app-server</code> so{' '}
            <code className="text-[10px]">{cfg?.upstreamListenUrl ?? 'ws://127.0.0.1:4500'}</code> is listening.{' '}
            <Link to="/settings" className="text-primary underline-offset-2 hover:underline">
              Settings → Codex app-server
            </Link>
            .
          </p>
        </div>
      </div>

      {error ? (
        <p className="text-destructive text-sm">{String((error as Error).message)}</p>
      ) : null}

      {cfg ? (
        <div className="glass-panel p-4 space-y-2 text-xs font-mono text-muted-foreground">
          <div>
            <span className="text-foreground font-medium">Proxy:</span>{' '}
            {cfg.proxyRunning ? (
              <span className="text-green-600 dark:text-green-400">running</span>
            ) : (
              <span className="text-amber-600 dark:text-amber-400">not running</span>
            )}
            {cfg.serveError ? <span className="text-destructive"> — {cfg.serveError}</span> : null}
          </div>
          <div>
            <span className="text-foreground font-medium">Browser WS:</span> {cfg.clientWsUrl}
          </div>
          <div>
            <span className="text-foreground font-medium">Upstream:</span> {cfg.upstreamListenUrl}
          </div>
          {!cfg.websocketsInstalled ? (
            <p className="text-amber-700 dark:text-amber-400 font-sans">
              Install dependency: <code className="text-[10px]">{cfg.installHint ?? 'pip install websockets'}</code>
              , then restart the dashboard.
            </p>
          ) : null}
          {cfg.disableReason && cfg.proxyWanted === false ? (
            <p className="font-sans text-muted-foreground">{cfg.disableReason}</p>
          ) : null}
        </div>
      ) : isLoading ? (
        <div className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading proxy configuration…
        </div>
      ) : null}

      {pendingList.length > 0 ? (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-foreground">Pending JSON-RPC requests (approvals)</h2>
          {pendingList.map((req) => (
            <div
              key={String(req.id)}
              className="rounded-lg border border-border bg-card/50 p-4 space-y-2 text-sm"
            >
              <div className="font-mono text-xs">
                <span className="text-muted-foreground">id</span> {String(req.id)}{' '}
                <span className="text-muted-foreground">method</span> {req.method}
              </div>
              <pre className="text-[11px] overflow-x-auto max-h-32 bg-muted/40 rounded p-2">
                {JSON.stringify(req.params ?? {}, null, 2)}
              </pre>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="default"
                  onClick={() => reply(req.id, { result: {} })}
                >
                  Approve (empty result)
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="destructive"
                  onClick={() =>
                    reply(req.id, {
                      error: { code: -32000, message: 'User declined' },
                    })
                  }
                >
                  Decline
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      <div className="glass-panel p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-foreground">Traffic log</h2>
          <Button type="button" variant="ghost" size="sm" onClick={() => setLog([])}>
            <Trash2 className="h-4 w-4 mr-1" />
            Clear
          </Button>
        </div>
        <div
          ref={scrollRef}
          className="h-[280px] w-full overflow-y-auto rounded-md border border-border/60 bg-muted/20 p-3 space-y-1 font-mono text-[11px] leading-relaxed"
        >
          {log.length === 0 ? (
            <p className="text-muted-foreground">No messages yet.</p>
          ) : (
            log.map((line, i) => (
              <div
                key={`${line.t}-${i}`}
                className={
                  line.dir === 'in'
                    ? 'text-sky-700 dark:text-sky-300'
                    : line.dir === 'out'
                      ? 'text-emerald-700 dark:text-emerald-300'
                      : 'text-muted-foreground'
                }
              >
                <span className="opacity-60 mr-2">{line.dir}</span>
                <span className="break-all whitespace-pre-wrap">{line.text}</span>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="glass-panel p-4 space-y-3">
        <h2 className="text-sm font-semibold text-foreground">Send JSON-RPC</h2>
        <Textarea
          value={outgoing}
          onChange={(e) => setOutgoing(e.target.value)}
          placeholder='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{…}}'
          className="min-h-[120px] font-mono text-xs"
        />
        <Button type="button" onClick={() => sendJson(outgoing)} disabled={wsState !== 'open'}>
          <Send className="h-4 w-4 mr-1" />
          Send
        </Button>
        <p className="text-xs text-muted-foreground">
          Use <code className="text-[10px]">codex app-server generate-json-schema</code> or OpenAI docs for methods (
          <code className="text-[10px]">initialize</code>, <code className="text-[10px]">thread/start</code>,{' '}
          <code className="text-[10px]">turn/start</code>, …).
        </p>
      </div>
    </div>
  );
}
