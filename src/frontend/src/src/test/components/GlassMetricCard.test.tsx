import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { Activity } from 'lucide-react';

describe('GlassMetricCard', () => {
  describe('required props', () => {
    it('renders the title', () => {
      render(<GlassMetricCard title="Test Metric" value={42} />);
      expect(screen.getByText('Test Metric')).toBeDefined();
    });

    it('renders numeric values', () => {
      render(<GlassMetricCard title="Count" value={42} />);
      expect(screen.getByText('42')).toBeDefined();
    });

    it('renders string values', () => {
      render(<GlassMetricCard title="Status" value="Online" />);
      expect(screen.getByText('Online')).toBeDefined();
    });
  });

  describe('optional props', () => {
    it('renders subtitle when provided', () => {
      render(<GlassMetricCard title="Metric" value={100} subtitle="Last 24h" />);
      expect(screen.getByText('Last 24h')).toBeDefined();
    });

    it('does not render subtitle when omitted', () => {
      render(<GlassMetricCard title="Metric" value={100} />);
      expect(screen.queryByText('Last 24h')).toBeNull();
    });

    it('renders icon when provided', () => {
      const { container } = render(
        <GlassMetricCard title="Metric" value={100} icon={Activity} />
      );
      // Icon renders as an SVG
      const svgs = container.querySelectorAll('svg');
      expect(svgs.length).toBeGreaterThanOrEqual(1);
    });

    it('renders positive trend', () => {
      render(
        <GlassMetricCard
          title="Metric"
          value={100}
          trend={{ value: 12, label: 'vs last week' }}
        />
      );
      expect(screen.getByText('+12%')).toBeDefined();
      expect(screen.getByText('vs last week')).toBeDefined();
    });

    it('renders negative trend', () => {
      render(
        <GlassMetricCard
          title="Metric"
          value={100}
          trend={{ value: -5, label: 'vs yesterday' }}
        />
      );
      expect(screen.getByText('-5%')).toBeDefined();
    });

    it('renders trend with custom suffix', () => {
      render(
        <GlassMetricCard
          title="Metric"
          value={100}
          trend={{ value: 3, label: 'growth', suffix: 'pts' }}
        />
      );
      expect(screen.getByText('+3pts')).toBeDefined();
    });

    it('renders children', () => {
      render(
        <GlassMetricCard title="Metric" value={100}>
          <span data-testid="child">Child Content</span>
        </GlassMetricCard>
      );
      expect(screen.getByTestId('child')).toBeDefined();
    });
  });

  describe('styling', () => {
    it('applies custom className', () => {
      const { container } = render(
        <GlassMetricCard title="Metric" value={100} className="custom-class" />
      );
      const card = container.firstChild as HTMLElement;
      expect(card.className).toContain('custom-class');
    });

    it('applies glow class when glow=true', () => {
      const { container } = render(
        <GlassMetricCard title="Metric" value={100} glow />
      );
      const card = container.firstChild as HTMLElement;
      expect(card.className).toContain('glass-card-glow');
    });

    it('applies standard glass-card class when glow=false', () => {
      const { container } = render(
        <GlassMetricCard title="Metric" value={100} />
      );
      const card = container.firstChild as HTMLElement;
      expect(card.className).toContain('glass-card');
    });
  });
});
