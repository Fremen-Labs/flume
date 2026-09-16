import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TooltipProvider } from '@/components/ui/tooltip';
import CoreOverview from '@/pages/CoreOverview';

describe('CoreOverview', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          service: 'flume-core',
          gateway: { status: 'ok' },
          elasticsearch: { status: 'ok' },
          openbao: { status: 'ok' },
          nodes: 2,
        }),
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders stack cards from /api/stack', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <CoreOverview />
        </TooltipProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByText('Core stack')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText('ok').length).toBeGreaterThan(0);
    });
    expect(screen.getByText('2')).toBeInTheDocument();
  });
});
