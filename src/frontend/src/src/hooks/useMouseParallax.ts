import { useEffect, useState, useCallback } from 'react';

export function useMouseParallax(intensity: number = 0.02) {
  const [offset, setOffset] = useState({ x: 0, y: 0 });

  const handleMouseMove = useCallback((e: MouseEvent) => {
    const x = (e.clientX / window.innerWidth - 0.5) * intensity * 100;
    const y = (e.clientY / window.innerHeight - 0.5) * intensity * 100;
    setOffset({ x, y });
  }, [intensity]);

  useEffect(() => {
    window.addEventListener('mousemove', handleMouseMove);
    return () => window.removeEventListener('mousemove', handleMouseMove);
  }, [handleMouseMove]);

  return offset;
}
