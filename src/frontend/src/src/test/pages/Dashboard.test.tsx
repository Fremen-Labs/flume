import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Dashboard from '@/pages/Dashboard';
import * as useSnapshotHook from '@/hooks/useSnapshot';
import * as useTelemetryHook from '@/hooks/useTelemetry';
import { TooltipProvider } from '@/components/ui/tooltip';

vi.mock('@/hooks/useSnapshot');
vi.mock('@/hooks/useTelemetry');

describe('Dashboard Page', () => {
  beforeEach(() => {
    vi.mocked(useSnapshotHook.useSnapshot).mockReturnValue({
      data: {
        projects: [
          { id: 'proj-1', name: 'Test Project 1', path: '/tmp/test1' }
        ],
        workers: [
          { name: 'worker-1', status: 'active', role: 'planner' }
        ],
        tasks: [
          { id: 'task-1', status: 'running', title: 'Running task' },
          { id: 'task-2', status: 'blocked', title: 'Blocked task' },
          { id: 'task-3', status: 'done', title: 'Completed task' },
        ],
        failures: []
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as any);

    vi.mocked(useTelemetryHook.useTelemetry).mockReturnValue({
      data: {
        flume_tasks_blocked_total: 1,
        flume_concurrency_throttled_total: 0,
      },
      isLoading: false,
    } as any);
  });

  it('renders the command center header', () => {
    render(<TooltipProvider><MemoryRouter><Dashboard /></MemoryRouter></TooltipProvider>);
    expect(screen.getByText('AI Project Command Center')).toBeDefined();
  });

  it('renders aggregated task metrics', () => {
    render(<TooltipProvider><MemoryRouter><Dashboard /></MemoryRouter></TooltipProvider>);
    // 1 total project, 1 worker, 1 in queue (well, 0 in queue based on mock, let's just check the cards)
    expect(screen.getByText('Tasks Running')).toBeDefined();
    expect(screen.getByText('Blocked Issues')).toBeDefined();
    expect(screen.getByText('Tasks Completed')).toBeDefined();
  });

  it('renders the pipeline stages', () => {
    render(<TooltipProvider><MemoryRouter><Dashboard /></MemoryRouter></TooltipProvider>);
    expect(screen.getByText('Work Pipeline')).toBeDefined();
    expect(screen.getByText('Planned')).toBeDefined();
    expect(screen.getByText('Running')).toBeDefined();
    expect(screen.getByText('Done')).toBeDefined();
    expect(screen.getByText('Blocked')).toBeDefined();
  });

  it('renders active workers list', () => {
    render(<TooltipProvider><MemoryRouter><Dashboard /></MemoryRouter></TooltipProvider>);
    expect(screen.getByText('Active Workers')).toBeDefined();
    expect(screen.getByText('worker-1')).toBeDefined();
  });

  it('renders projects list', () => {
    render(<TooltipProvider><MemoryRouter><Dashboard /></MemoryRouter></TooltipProvider>);
    expect(screen.getByText('Test Project 1')).toBeDefined();
  });
});
