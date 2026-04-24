import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProgressRing } from '@/components/ProgressRing';

describe('ProgressRing', () => {
  describe('value display', () => {
    it('renders the percentage value as text', () => {
      render(<ProgressRing value={75} />);
      expect(screen.getByText('75%')).toBeDefined();
    });

    it('renders 0% correctly', () => {
      render(<ProgressRing value={0} />);
      expect(screen.getByText('0%')).toBeDefined();
    });

    it('renders 100% correctly', () => {
      render(<ProgressRing value={100} />);
      expect(screen.getByText('100%')).toBeDefined();
    });
  });

  describe('SVG rendering', () => {
    it('renders an SVG element', () => {
      const { container } = render(<ProgressRing value={50} />);
      const svg = container.querySelector('svg');
      expect(svg).toBeDefined();
      expect(svg).not.toBeNull();
    });

    it('renders two circle elements (track + progress)', () => {
      const { container } = render(<ProgressRing value={50} />);
      const circles = container.querySelectorAll('circle');
      expect(circles.length).toBe(2);
    });

    it('uses default size of 40', () => {
      const { container } = render(<ProgressRing value={50} />);
      const svg = container.querySelector('svg');
      expect(svg?.getAttribute('width')).toBe('40');
      expect(svg?.getAttribute('height')).toBe('40');
    });

    it('accepts custom size', () => {
      const { container } = render(<ProgressRing value={50} size={60} />);
      const svg = container.querySelector('svg');
      expect(svg?.getAttribute('width')).toBe('60');
      expect(svg?.getAttribute('height')).toBe('60');
    });
  });

  describe('color thresholds', () => {
    it('uses success color for >= 75%', () => {
      const { container } = render(<ProgressRing value={80} />);
      const progressCircle = container.querySelectorAll('circle')[1];
      expect(progressCircle.classList.contains('stroke-success')).toBe(true);
    });

    it('uses primary color for 40-74%', () => {
      const { container } = render(<ProgressRing value={50} />);
      const progressCircle = container.querySelectorAll('circle')[1];
      expect(progressCircle.classList.contains('stroke-primary')).toBe(true);
    });

    it('uses warning color for < 40%', () => {
      const { container } = render(<ProgressRing value={20} />);
      const progressCircle = container.querySelectorAll('circle')[1];
      expect(progressCircle.classList.contains('stroke-warning')).toBe(true);
    });
  });
});
