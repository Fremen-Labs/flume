import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import NodesOverview from '@/pages/NodesOverview';

describe('NodesOverview Page', () => {
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

      if (url === '/api/nodes') {
        return Promise.resolve(createMockResponse({
          nodes: [
            {
              id: 'node-1',
              host: '127.0.0.1:11434',
              health: { status: 'healthy', latency_ms: 12 },
              capabilities: { memory_gb: 32, max_context: 8192 }
            }
          ],
          count: 1
        }));
      }
      if (url === '/api/routing-policy') {
        return Promise.resolve(createMockResponse({
          mode: 'hybrid',
          strict_pinning: false,
          frontier_mix: []
        }));
      }
      if (url === '/api/frontier-models') {
        return Promise.resolve(createMockResponse({
          catalogs: []
        }));
      }
      return Promise.resolve(createMockResponse({}));
    });
  });

  const renderPage = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <NodesOverview />
        </MemoryRouter>
      </QueryClientProvider>
    );

  it('renders the overview header', async () => {
    renderPage();
    expect(screen.getByText('Node Mesh')).toBeDefined();
    
    // Check for the node card from API
    await waitFor(() => {
      expect(screen.getByText('127.0.0.1:11434')).toBeDefined();
    });
  });

  it('renders node health status', async () => {
    renderPage();
    await waitFor(() => {
      expect(screen.getAllByText('Healthy').length).toBeGreaterThan(0);
    });
  });

  it('opens add node modal', async () => {
    renderPage();
    
    const addBtn = screen.getByText('Add Node');
    fireEvent.click(addBtn);
    
    expect(screen.getAllByText('Register Node').length).toBeGreaterThan(0);
    expect(screen.getByPlaceholderText('192.168.1.50:11434')).toBeDefined();
  });
});
