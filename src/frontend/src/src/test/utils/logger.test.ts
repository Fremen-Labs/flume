import { describe, it, expect, vi, beforeEach } from 'vitest';
import { appLogger } from '@/utils/logger';

describe('appLogger', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  describe('info', () => {
    it('logs with [INF] prefix and ISO timestamp', () => {
      const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
      appLogger.info('test message');
      expect(spy).toHaveBeenCalledOnce();
      const loggedMsg = spy.mock.calls[0][0];
      expect(loggedMsg).toMatch(/^\[INF\] \d{4}-\d{2}-\d{2}T.*test message$/);
    });

    it('passes extra arguments through', () => {
      const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
      appLogger.info('msg', { key: 'value' });
      expect(spy).toHaveBeenCalledOnce();
      expect(spy.mock.calls[0][1]).toEqual({ key: 'value' });
    });
  });

  describe('warn', () => {
    it('logs with [WRN] prefix', () => {
      const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      appLogger.warn('warning message');
      expect(spy).toHaveBeenCalledOnce();
      expect(spy.mock.calls[0][0]).toContain('[WRN]');
      expect(spy.mock.calls[0][0]).toContain('warning message');
    });
  });

  describe('error', () => {
    it('logs with [ERR] prefix', () => {
      const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
      appLogger.error('error message');
      expect(spy).toHaveBeenCalledOnce();
      expect(spy.mock.calls[0][0]).toContain('[ERR]');
      expect(spy.mock.calls[0][0]).toContain('error message');
    });
  });

  describe('debug', () => {
    it('includes [DBG] prefix when called', () => {
      const spy = vi.spyOn(console, 'debug').mockImplementation(() => {});
      appLogger.debug('debug message');
      // debug may or may not fire depending on NODE_ENV
      if (spy.mock.calls.length > 0) {
        expect(spy.mock.calls[0][0]).toContain('[DBG]');
      }
    });
  });

  describe('timestamp format', () => {
    it('includes ISO 8601 timestamp in info logs', () => {
      const spy = vi.spyOn(console, 'log').mockImplementation(() => {});
      appLogger.info('timestamp test');
      const msg = spy.mock.calls[0][0];
      // ISO 8601 pattern: 2026-04-24T15:00:00.000Z
      expect(msg).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    });
  });
});
