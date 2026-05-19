/**
 * LogLoom-Compatible Centralized Logger for Flume Frontend
 *
 * Every log entry is enriched with structured metadata that correlates with
 * the LogLoom AST graph: module name, function name, and severity level.
 *
 * Usage:
 *   import { createLogger } from '@/utils/logger';
 *   const log = createLogger('hooks.useSnapshot');
 *   log.info('fetchSnapshot', 'Snapshot loaded', { count: 42 });
 *
 * Legacy compatibility:
 *   import { appLogger } from '@/utils/logger';  // module='app'
 */

/* ── Types ─────────────────────────────────────────────────────────────── */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogEntry {
  /** ISO-8601 timestamp */
  ts: string;
  /** Severity level */
  level: LogLevel;
  /** LogLoom module path (e.g. 'hooks.useSnapshot') */
  'logloom.module': string;
  /** Function name (e.g. 'fetchSnapshot') */
  'logloom.function': string;
  /** Human-readable message */
  msg: string;
  /** Optional structured context */
  ctx?: Record<string, unknown>;
}

/* ── Transport ─────────────────────────────────────────────────────────── */

/**
 * LogLoom transport — buffers log entries and flushes them to the backend
 * in batches to avoid per-entry HTTP overhead.
 */
const _BUFFER: LogEntry[] = [];
const _FLUSH_INTERVAL_MS = 5_000;
const _FLUSH_MAX_SIZE = 50;
let _flushTimer: ReturnType<typeof setTimeout> | null = null;
let _transportEnabled =
  typeof window !== 'undefined' &&
  // Only enable transport in production or when explicitly opted in.
  (import.meta.env.PROD || import.meta.env.VITE_LOGLOOM_TRANSPORT === 'true');

function _scheduleFlush(): void {
  if (_flushTimer !== null) return;
  _flushTimer = setTimeout(() => {
    _flushTimer = null;
    _flushToBackend();
  }, _FLUSH_INTERVAL_MS);
}

async function _flushToBackend(): Promise<void> {
  if (_BUFFER.length === 0) return;
  const batch = _BUFFER.splice(0, _FLUSH_MAX_SIZE);
  try {
    await fetch('/api/telemetry/logs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: batch }),
    });
  } catch {
    // Best-effort — logs are not critical enough to retry.
  }
}

/* ── Console Writers ───────────────────────────────────────────────────── */

const LEVEL_PREFIX: Record<LogLevel, string> = {
  debug: '[DBG]',
  info: '[INF]',
  warn: '[WRN]',
  error: '[ERR]',
};

const CONSOLE_FN: Record<LogLevel, (...args: unknown[]) => void> = {
  // eslint-disable-next-line no-console
  debug: console.debug.bind(console),
  // eslint-disable-next-line no-console
  info: console.log.bind(console),
  // eslint-disable-next-line no-console
  warn: console.warn.bind(console),
  // eslint-disable-next-line no-console
  error: console.error.bind(console),
};

function _emit(entry: LogEntry): void {
  // 1. Always write to browser console
  const prefix = `${LEVEL_PREFIX[entry.level]} ${entry.ts} [${entry['logloom.module']}::${entry['logloom.function']}]`;
  if (entry.ctx && Object.keys(entry.ctx).length > 0) {
    CONSOLE_FN[entry.level](`${prefix} ${entry.msg}`, entry.ctx);
  } else {
    CONSOLE_FN[entry.level](`${prefix} ${entry.msg}`);
  }

  // 2. Buffer for backend transport
  if (_transportEnabled) {
    _BUFFER.push(entry);
    if (_BUFFER.length >= _FLUSH_MAX_SIZE) {
      _flushToBackend();
    } else {
      _scheduleFlush();
    }
  }
}

/* ── Logger Factory ────────────────────────────────────────────────────── */

export interface Logger {
  debug: (fn: string, msg: string, ctx?: Record<string, unknown>) => void;
  info: (fn: string, msg: string, ctx?: Record<string, unknown>) => void;
  warn: (fn: string, msg: string, ctx?: Record<string, unknown>) => void;
  error: (fn: string, msg: string, ctx?: Record<string, unknown>) => void;
}

/**
 * Create a module-scoped logger. The `moduleName` is stored in every log
 * entry's `logloom.module` field for AST correlation.
 *
 * @example
 *   const log = createLogger('hooks.useSnapshot');
 *   log.info('fetchSnapshot', 'Loaded snapshot', { tasks: 42 });
 */
export function createLogger(moduleName: string): Logger {
  const makeWriter = (level: LogLevel) => {
    return (fn: string, msg: string, ctx?: Record<string, unknown>): void => {
      // Suppress debug in production unless VITE_LOG_LEVEL=debug
      if (level === 'debug' && import.meta.env.PROD && import.meta.env.VITE_LOG_LEVEL !== 'debug') {
        return;
      }
      _emit({
        ts: new Date().toISOString(),
        level,
        'logloom.module': moduleName,
        'logloom.function': fn,
        msg,
        ctx,
      });
    };
  };

  return {
    debug: makeWriter('debug'),
    info: makeWriter('info'),
    warn: makeWriter('warn'),
    error: makeWriter('error'),
  };
}

/* ── Legacy appLogger (backwards compatible) ──────────────────────────── */

const _legacyLog = createLogger('app');

/**
 * @deprecated Use `createLogger('module.name')` for LogLoom-enriched logging.
 *
 * Preserved for backward compatibility with existing code that imports
 * `appLogger` from `@/utils/logger`.
 */
export const appLogger = {
  info: (msg: string, ...args: unknown[]) => {
    _legacyLog.info('appLogger', msg, args.length ? { args } : undefined);
  },
  warn: (msg: string, ...args: unknown[]) => {
    _legacyLog.warn('appLogger', msg, args.length ? { args } : undefined);
  },
  error: (msg: string, ...args: unknown[]) => {
    _legacyLog.error('appLogger', msg, args.length ? { args } : undefined);
  },
  debug: (msg: string, ...args: unknown[]) => {
    _legacyLog.debug('appLogger', msg, args.length ? { args } : undefined);
  },
};

/* ── Lifecycle ─────────────────────────────────────────────────────────── */

/**
 * Flush all buffered log entries immediately (call on page unload).
 */
export function flushLogs(): void {
  _flushToBackend();
}

/**
 * Enable or disable backend transport at runtime.
 */
export function setTransportEnabled(enabled: boolean): void {
  _transportEnabled = enabled;
}
