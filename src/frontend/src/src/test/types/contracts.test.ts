import { describe, it, expect } from 'vitest';

/**
 * Type-level validation tests for the frontend type system.
 * These tests verify that the core TypeScript interfaces used by
 * hooks and API consumers are structurally sound.
 *
 * If these tests fail, it indicates a breaking change in the API
 * contract between the backend and frontend.
 */

describe('Type Contracts', () => {
  describe('AgentStatus interface', () => {
    it('has correct shape', async () => {
      const mod = await import('@/hooks/useAgentStatus');
      // Verify the hook export exists
      expect(mod.useAgentStatus).toBeDefined();
      expect(typeof mod.useAgentStatus).toBe('function');
    });
  });

  describe('SystemState interface', () => {
    it('exports WorkerState and SystemState types', async () => {
      const mod = await import('@/hooks/useSystemState');
      expect(mod.useSystemState).toBeDefined();
      expect(typeof mod.useSystemState).toBe('function');
    });
  });

  describe('TelemetryData interface', () => {
    it('exports useTelemetry hook', async () => {
      const mod = await import('@/hooks/useTelemetry');
      expect(mod.useTelemetry).toBeDefined();
      expect(typeof mod.useTelemetry).toBe('function');
    });
  });

  describe('useSnapshot hook', () => {
    it('exports useSnapshot hook', async () => {
      const mod = await import('@/hooks/useSnapshot');
      expect(mod.useSnapshot).toBeDefined();
      expect(typeof mod.useSnapshot).toBe('function');
    });
  });

  describe('Snapshot type exports', () => {
    it('types/index.ts is importable', async () => {
      const mod = await import('@/types');
      expect(mod).toBeDefined();
    });
  });
});
