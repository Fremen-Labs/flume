import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import SettingsPage from '@/pages/SettingsPage';

// We need to mock the Theme context so the component doesn't crash trying to use it
vi.mock('@/hooks/useTheme', () => ({
  useTheme: () => ({
    theme: 'dark',
    skin: 'default',
    toggleTheme: vi.fn(),
    setSkin: vi.fn(),
  }),
}));

describe('SettingsPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.restoreAllMocks();

    global.fetch = vi.fn().mockImplementation((url: string) => {
      const createMockResponse = (data: any) => ({
        ok: true,
        json: () => Promise.resolve(data),
        text: () => Promise.resolve(JSON.stringify(data)),
      });

      if (url.includes('/api/settings/llm')) {
        return Promise.resolve(createMockResponse({
          settings: {
            provider: 'ollama',
            model: 'llama3',
            authMode: 'api_key',
            apiKey: '***'
          },
          catalog: [
            { id: 'ollama', name: 'Ollama (Local)', models: [{ id: 'llama3', name: 'Llama 3' }] }
          ],
          credentials: []
        }));
      }
      if (url.includes('/api/settings/repos')) {
        return Promise.resolve(createMockResponse({
          settings: {
            githubTokens: [{ id: 't1', label: 'Work', hasToken: true, tokenSuffix: 'abcd' }]
          }
        }));
      }
      if (url.includes('/api/settings/system')) {
        return Promise.resolve(createMockResponse({
          es_url: 'http://localhost:9200',
          es_api_key: '***',
          openbao_url: 'http://localhost:8200',
          vault_token: '***'
        }));
      }
      if (url.includes('/api/exo-status')) {
        return Promise.resolve(createMockResponse({ active: false }));
      }
      if (url.includes('/api/codex-app-server/status')) {
        return Promise.resolve(createMockResponse({ tcpReachable: true }));
      }
      return Promise.resolve(createMockResponse({}));
    });
  });

  const renderPage = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <SettingsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

  it('renders settings header', async () => {
    renderPage();
    expect(await screen.findByText('Configure LLM providers, models, and authentication.')).toBeDefined();
  });

  it('displays masked sensitive fields in system settings', async () => {
    renderPage();
    
    // We have to expand the accordion first if it's not open by default
    // The "System Infrastructure" accordion has the title "System Infrastructure"
    const systemTrigger = await screen.findByText('System Infrastructure');
    fireEvent.click(systemTrigger);
    
    await waitFor(() => {
      // The ES api key input should display '***'
      const inputs = screen.getAllByPlaceholderText('Elasticsearch Key');
      expect(inputs.length).toBeGreaterThan(0);
      expect((inputs[0] as HTMLInputElement).value).toBe('');
      
      const vaultInputs = screen.getAllByPlaceholderText('Vault Root Token');
      expect(vaultInputs.length).toBeGreaterThan(0);
      expect((vaultInputs[0] as HTMLInputElement).value).toBe('***');
    });
  });

  it('displays github tokens in repo settings', async () => {
    renderPage();
    
    const repoTrigger = await screen.findByText('Repo credentials');
    fireEvent.click(repoTrigger);
    
    await waitFor(() => {
      // Label 'Work' from the mock
      expect(screen.getByText('Work')).toBeDefined();
      // Masked token '···abcd'
      expect(screen.getByText('···abcd')).toBeDefined();
    });
  });
});
