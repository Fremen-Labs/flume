/**
 * Centralized, FAANG-grade logger for the Flume frontend.
 *
 * Goals:
 * - Eliminate direct console.* usage
 * - Structured logging with context (current task, user, etc.)
 * - Easy integration with backend Logloom-enriched logs
 * - Batching + transport for production
 * - Type-safe and pleasant to use
 */

export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogContext {
  taskId?: string;
  userId?: string;
  sessionId?: string;
  llNode?: string;           // Logloom node ID when known
  component?: string;
  [key: string]: any;
}

interface LogEntry {
  timestamp: string;
  level: LogLevel;
  message: string;
  context: LogContext;
  error?: {
    message: string;
    stack?: string;
  };
}

class FrontendLogger {
  private context: LogContext = {};
  private buffer: LogEntry[] = [];
  private flushTimer: NodeJS.Timeout | null = null;
  private readonly flushInterval = 2000; // 2s batching in prod

  constructor() {
    // In production we could send to /api/logs or a beacon endpoint
    if (typeof window !== 'undefined' && process.env.NODE_ENV === 'production') {
      window.addEventListener('beforeunload', () => this.flush(true));
    }
  }

  withContext(newContext: Partial<LogContext>): FrontendLogger {
    const child = new FrontendLogger();
    child.context = { ...this.context, ...newContext };
    return child;
  }

  private log(level: LogLevel, message: string, data?: any, error?: Error) {
    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      level,
      message,
      context: { ...this.context, ...data },
    };

    if (error) {
      entry.error = {
        message: error.message,
        stack: error.stack,
      };
    }

    // Always log to console in dev (with nice formatting)
    if (process.env.NODE_ENV !== 'production') {
      const consoleMethod = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
      consoleMethod(`[${level.toUpperCase()}] ${message}`, entry.context, error || '');
    } else {
      // In production, buffer for batch sending
      this.buffer.push(entry);
      this.scheduleFlush();
    }

    // Also send to backend structured log endpoint if we have task context
    if (this.context.taskId) {
      this.sendToBackend(entry);
    }
  }

  private scheduleFlush() {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => this.flush(), this.flushInterval);
  }

  private async flush(immediate = false) {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }

    if (this.buffer.length === 0) return;

    const toSend = [...this.buffer];
    this.buffer = [];

    // In a real FAANG setup this would go to a proper log ingestion service
    // For now we send to the backend /api/logs if it exists
    try {
      if (typeof fetch !== 'undefined') {
        fetch('/api/logs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ logs: toSend }),
          keepalive: immediate,
        }).catch(() => {
          // Fail silently in production logging
        });
      }
    } catch {
      // ignore
    }
  }

  private sendToBackend(entry: LogEntry) {
    // Fire-and-forget to backend structured logging (enriched server-side with Logloom context)
    if (typeof fetch === 'undefined') return;

    fetch('/api/logs/structured', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    }).catch(() => {});
  }

  debug(message: string, data?: any) {
    this.log('debug', message, data);
  }

  info(message: string, data?: any) {
    this.log('info', message, data);
  }

  warn(message: string, data?: any) {
    this.log('warn', message, data);
  }

  error(message: string, error?: Error | any, data?: any) {
    const err = error instanceof Error ? error : undefined;
    const extra = error instanceof Error ? data : error;
    this.log('error', message, extra, err);
  }

  // Convenience for agent reasoning / thoughts
  agentReasoning(taskId: string, reasoning: string, metadata?: any) {
    this.withContext({ taskId, llNode: metadata?.llNode })
      .info('agent reasoning', { reasoning, ...metadata });
  }
}

export const logger = new FrontendLogger();

// Global error handler to catch uncaught issues
if (typeof window !== 'undefined') {
  window.addEventListener('error', (event) => {
    logger.error('Uncaught error', event.error, {
      filename: event.filename,
      lineno: event.lineno,
    });
  });

  window.addEventListener('unhandledrejection', (event) => {
    logger.error('Unhandled promise rejection', event.reason);
  });
}

// Helper to replace console.log during migration
export const legacyConsole = {
  log: (...args: any[]) => logger.info(args.join(' ')),
  warn: (...args: any[]) => logger.warn(args.join(' ')),
  error: (...args: any[]) => logger.error(args.join(' ')),
};
