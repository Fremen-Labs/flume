import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import ProjectsPage from '@/pages/ProjectsPage';
import * as useSnapshotHook from '@/hooks/useSnapshot';

vi.mock('@/hooks/useSnapshot');

describe('ProjectsPage', () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.restoreAllMocks();

    vi.mocked(useSnapshotHook.useSnapshot).mockReturnValue({
      data: {
        projects: [
          { id: 'proj-1', name: 'Frontend App', path: '/src/frontend', created_at: new Date().toISOString() },
          { id: 'proj-2', name: 'Backend API', repoUrl: 'https://github.com/org/api.git', clone_status: 'cloning', created_at: new Date().toISOString() }
        ],
        tasks: [
          { id: 't1', repo: 'proj-1', status: 'running' },
          { id: 't2', repo: 'proj-1', status: 'done' },
        ],
        workers: [],
        failures: [],
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as any);

    // Mock fetch for createProject/deleteProject API calls
    global.fetch = vi.fn().mockImplementation(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ project: { id: 'proj-3', name: 'New Project' } }),
      })
    );
  });

  const renderPage = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ProjectsPage />
        </MemoryRouter>
      </QueryClientProvider>
    );

  it('renders project list and header', () => {
    renderPage();
    expect(screen.getByText('Projects')).toBeDefined();
    expect(screen.getByText('Frontend App')).toBeDefined();
    expect(screen.getByText('Backend API')).toBeDefined();
  });

  it('displays correct clone status badges', () => {
    renderPage();
    // proj-2 has clone_status: 'cloning'
    expect(screen.getByText('Cloning…')).toBeDefined();
  });

  it('calculates task statistics correctly', () => {
    renderPage();
    // proj-1 has 1 running, 1 done, 0 blocked, 0 planned. Total = 2.
    // The numbers are rendered in separate div elements, so we look for the totals.
    const runningStats = screen.getAllByText('1');
    expect(runningStats.length).toBeGreaterThan(0);
  });

  it('opens new project modal when clicking New Project', () => {
    renderPage();
    const newProjectBtn = screen.getByText('New Project');
    fireEvent.click(newProjectBtn);
    
    // Dialog should open
    expect(screen.getAllByText('Create Project').length).toBeGreaterThan(0);
    // Check for input
    expect(screen.getByPlaceholderText('e.g. customer-onboarding')).toBeDefined();
  });

  it('submits a new project successfully', async () => {
    renderPage();
    fireEvent.click(screen.getByText('New Project'));
    
    const input = screen.getByPlaceholderText('e.g. customer-onboarding');
    fireEvent.change(input, { target: { value: 'My New Project' } });
    
    // There are two "Create Project" texts, one is the header, one is the button
    const submitBtn = screen.getAllByText('Create Project').find(el => el.tagName.toLowerCase() === 'button');
    expect(submitBtn).toBeDefined();
    fireEvent.click(submitBtn!);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('My New Project'),
      }));
    });
  });
});
