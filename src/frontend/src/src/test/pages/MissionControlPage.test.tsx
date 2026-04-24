import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import MissionControlPage from '@/pages/MissionControlPage';
import * as useSystemStateHook from '@/hooks/useSystemState';

vi.mock('@/hooks/useSystemState');

describe('MissionControlPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.restoreAllMocks();

    vi.mocked(useSystemStateHook.useSystemState).mockReturnValue({
      workers: [
        { name: 'agent-1', role: 'planner', model: 'gpt-4o', status: 'active', execution_host: 'localhost', heartbeat_at: new Date().toISOString() }
      ],
      updated_at: new Date().toISOString(),
    } as any);

    global.fetch = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/settings/agent-models') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            roleIds: ['planner', 'coder'],
            effective: {
              planner: { model: 'gpt-4o', provider: 'openai' },
              coder: { model: 'claude-3-5-sonnet', provider: 'anthropic' }
            },
            settingsProvider: 'ollama',
            defaultLlmModel: 'llama3',
            defaultExecutionHost: 'localhost'
          })
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({})
      });
    });
  });

  const renderPage = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MissionControlPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

  it('renders live mission radar components', async () => {
    renderPage();
    // Verify system state mock is working (LiveMissionRadar should show 1 active worker)
    expect(screen.getByText('Mission Control')).toBeDefined();
    
    // We expect "agent-1" or "Active Agents: 1" from the radar
    // Looking for the role name or agent name might depend on the internal radar logic,
    // but we can definitely look for "agent-models" data:
    await waitFor(() => {
      expect(screen.getByText('agent-1')).toBeDefined();
    });
  });

  it('loads and displays role configuration from API', async () => {
    renderPage();
    await waitFor(() => {
      // The API returns planner and coder roles
      expect(screen.getByText(/planner/i)).toBeDefined();
    });
  });
});
