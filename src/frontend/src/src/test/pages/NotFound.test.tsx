import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NotFound from '@/pages/NotFound';

describe('NotFound Page', () => {
  const renderWithRouter = () =>
    render(
      <MemoryRouter>
        <NotFound />
      </MemoryRouter>
    );

  it('renders the 404 indicator', () => {
    renderWithRouter();
    expect(screen.getByText('404')).toBeDefined();
  });

  it('displays a user-friendly message', () => {
    renderWithRouter();
    // Should contain some form of "not found" messaging
    const container = document.body.textContent || '';
    expect(container.toLowerCase()).toContain('not found');
  });

  it('provides a link back to the home page', () => {
    const { container } = renderWithRouter();
    const links = container.querySelectorAll('a');
    const homeLink = Array.from(links).find(
      (a) => a.getAttribute('href') === '/'
    );
    expect(homeLink).toBeDefined();
  });
});
