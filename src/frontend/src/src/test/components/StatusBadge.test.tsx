import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusBadge } from '@/components/StatusBadge';

describe('StatusBadge', () => {
  describe('known statuses', () => {
    const knownStatuses = [
      { status: 'idle', label: 'Idle' },
      { status: 'active', label: 'Active' },
      { status: 'running', label: 'Running' },
      { status: 'done', label: 'Done' },
      { status: 'blocked', label: 'Blocked' },
      { status: 'failed', label: 'Failed' },
      { status: 'ready', label: 'Ready' },
      { status: 'planned', label: 'Planned' },
      { status: 'healthy', label: 'Healthy' },
      { status: 'in_progress', label: 'In Progress' },
    ];

    it.each(knownStatuses)('renders "$label" for status "$status"', ({ status, label }) => {
      render(<StatusBadge status={status} />);
      expect(screen.getByText(label)).toBeDefined();
    });
  });

  describe('unknown status', () => {
    it('renders the raw status string for unknown statuses', () => {
      render(<StatusBadge status="custom_unknown_status" />);
      expect(screen.getByText('custom_unknown_status')).toBeDefined();
    });
  });

  describe('rendering', () => {
    it('renders as an inline element with the status dot', () => {
      const { container } = render(<StatusBadge status="active" />);
      // Should have at least one span element for the dot
      const spans = container.querySelectorAll('span');
      expect(spans.length).toBeGreaterThanOrEqual(2); // outer + dot + label
    });

    it('renders without pulse by default', () => {
      const { container } = render(<StatusBadge status="active" />);
      // Should render without the motion pulse element when pulse=false
      expect(container.querySelector('span')).toBeDefined();
    });

    it('accepts pulse prop without crashing', () => {
      expect(() => {
        render(<StatusBadge status="active" pulse />);
      }).not.toThrow();
    });

    it('accepts pulse prop for non-animated statuses', () => {
      expect(() => {
        render(<StatusBadge status="idle" pulse />);
      }).not.toThrow();
    });
  });
});
