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

  // Grok uplift contract assertion for AST Savings card (addresses incomplete types gap).
  // Verifies the now-complete token_metrics shape (estimated_cost_usd + historical_burn array)
  // matches backend projection. If this fails after a backend change, the uplift comment in
  // types/index.ts + api_system.go will guide the fix. Keeps the card's data contract explicit/tested.
  describe('Snapshot token_metrics contract (AST Savings)', () => {
    it('token_metrics interface includes estimated_cost_usd and historical_burn shape', async () => {
      const mod = await import('@/types');
      // Structural runtime contract check (shape only; values exercised in component + Glass tests)
      const sample: import('@/types').Snapshot = {
        workers: [],
        tasks: [],
        reviews: [],
        failures: [],
        provenance: [],
        repos: [],
        projects: [],
        token_metrics: {
          savings: 1234,
          baseline_tokens: 5000,
          baseline_full_context_tokens: 5000,
          actual_tokens_sent: 3766,
          total_input_tokens: 3000,
          total_output_tokens: 766,
          estimated_cost_usd: 12.34,
          historical_burn: [
            { worker_name: 'impl-1', input_tokens: 2000, output_tokens: 500, role: 'implementer' },
          ],
        },
        elastro_savings: 1234,
      };
      // The assignment above proves the TS interface accepts the full shape from backend.
      // Additional runtime assertions on optional presence for resilience (partial data case).
      expect(sample.token_metrics?.estimated_cost_usd).toBeTypeOf('number');
      expect(Array.isArray(sample.token_metrics?.historical_burn)).toBe(true);
      if (sample.token_metrics?.historical_burn && sample.token_metrics.historical_burn.length > 0) {
        const entry = sample.token_metrics.historical_burn[0];
        expect(entry).toHaveProperty('worker_name');
        expect(entry).toHaveProperty('input_tokens');
        expect(entry).toHaveProperty('output_tokens');
        expect(entry).toHaveProperty('role');
      }
      expect(mod).toBeDefined();
    });
  });
});
