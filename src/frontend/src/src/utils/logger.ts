/**
 * Central Logger Utility for Flume Frontend
 * Ensure all standard UI component logs are routed through these methods 
 * rather than bare `console.error` or `console.log`.
 */

export const appLogger = {
  info: (msg: string, ...args: any[]) => {
    // eslint-disable-next-line no-console
    console.log(`[INF] ${new Date().toISOString()} ${msg}`, ...args);
  },
  warn: (msg: string, ...args: any[]) => {
    // eslint-disable-next-line no-console
    console.warn(`[WRN] ${new Date().toISOString()} ${msg}`, ...args);
  },
  error: (msg: string, ...args: any[]) => {
    // eslint-disable-next-line no-console
    console.error(`[ERR] ${new Date().toISOString()} ${msg}`, ...args);
  },
  debug: (msg: string, ...args: any[]) => {
    if (process.env.NODE_ENV === 'development') {
      // eslint-disable-next-line no-console
      console.debug(`[DBG] ${new Date().toISOString()} ${msg}`, ...args);
    }
  }
};
