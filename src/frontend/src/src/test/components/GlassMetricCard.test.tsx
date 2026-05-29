import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { GlassMetricCard } from '@/components/GlassMetricCard';
import { Activity } from 'lucide-react';
import { TooltipProvider } from '@/components/ui/tooltip';

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

  describe('resilience props (Grok uplift activation)', () => {
    it('renders loading skeleton and spinner row', () => {
      const { container } = render(<GlassMetricCard title="Metric" value={100} loading />);
      expect(container.querySelector('.animate-pulse')).toBeTruthy(); // Skeleton
      expect(screen.getByText('Loading…')).toBeDefined();
    });

    it('renders error banner with icon and em-dash value', () => {
      render(
        <GlassMetricCard title="Metric" value={100} error="Backend timeout" />
      );
      expect(screen.getByText('—')).toBeDefined();
      expect(screen.getByText('Backend timeout')).toBeDefined();
    });

    it('renders partial data warning indicator', () => {
      render(<GlassMetricCard title="Metric" value={42} partial />);
      expect(screen.getByText('PARTIAL DATA')).toBeDefined();
    });

    it('renders helpText as native title and info icon (wrapped in provider)', () => {
      const { container } = render(
        <TooltipProvider>
          <GlassMetricCard
            title="VRAM Pressure"
            value={7}
            helpText="Cumulative VRAM pressure events from Ollama probes."
          />
        </TooltipProvider>
      );
      const titleEl = screen.getByText('VRAM Pressure');
      expect(titleEl.getAttribute('title')).toContain('Cumulative VRAM');
      // Info icon SVG present
      const svgs = container.querySelectorAll('svg');
      expect(svgs.length).toBeGreaterThanOrEqual(2); // icon + info
    });

    it('renders secondary non-delta row consistently', () => {
      render(
        <GlassMetricCard
          title="System Memory"
          value="512MB"
          secondary={{ label: 'goroutines', value: 128 }}
        />
      );
      expect(screen.getByText('goroutines')).toBeDefined();
      expect(screen.getByText('128')).toBeDefined();
    });

    it('prioritizes loading over error/partial', () => {
      render(<GlassMetricCard title="X" value={1} loading error="boom" partial />);
      expect(screen.getByText('Loading…')).toBeDefined();
      expect(screen.queryByText('boom')).toBeNull();
    });
  });
});
