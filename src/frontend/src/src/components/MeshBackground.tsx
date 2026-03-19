import { motion } from 'framer-motion';
import { useMouseParallax } from '@/hooks/useMouseParallax';
import { useTheme } from '@/hooks/useTheme';

export function MeshBackground() {
  const bgOffset = useMouseParallax(0.01);
  const { theme } = useTheme();
  const isDark = theme === 'dark';

  return (
    <div className="fixed inset-0 overflow-hidden pointer-events-none z-0">
      {/* Layered gradient atmosphere */}
      <div
        className="absolute inset-0 transition-colors duration-500"
        style={{
          background: isDark
            ? `
              radial-gradient(ellipse 120% 80% at 20% 10%, hsl(230 60% 8%) 0%, transparent 60%),
              radial-gradient(ellipse 100% 60% at 80% 90%, hsl(200 50% 6%) 0%, transparent 50%),
              radial-gradient(ellipse 80% 40% at 50% 50%, hsl(260 30% 5%) 0%, transparent 40%),
              hsl(240 10% 2%)
            `
            : `
              radial-gradient(ellipse 120% 80% at 20% 10%, hsl(220 20% 94%) 0%, transparent 60%),
              radial-gradient(ellipse 100% 60% at 80% 90%, hsl(220 15% 93%) 0%, transparent 50%),
              hsl(0 0% 98%)
            `,
        }}
      />

      {/* Faint grid texture */}
      <div
        className="absolute inset-0"
        style={{
          opacity: isDark ? 0.03 : 0.06,
          backgroundImage: `
            linear-gradient(rgb(var(--grid-line-color)) 1px, transparent 1px),
            linear-gradient(90deg, rgb(var(--grid-line-color)) 1px, transparent 1px)
          `,
          backgroundSize: '60px 60px',
        }}
      />

      {/* Parallax light orbs */}
      <motion.div
        className="absolute inset-0"
        animate={{ x: bgOffset.x, y: bgOffset.y }}
        transition={{ type: 'spring', stiffness: 40, damping: 30 }}
      >
        <div
          className="absolute top-[-15%] left-[-5%] w-[55%] h-[55%] rounded-full blur-[140px] orb-pulse"
          style={{ background: isDark ? 'hsl(239 84% 67% / 0.05)' : 'hsl(220 20% 85% / 0.4)' }}
        />
        <div
          className="absolute bottom-[-10%] right-[-5%] w-[45%] h-[45%] rounded-full blur-[120px] orb-pulse"
          style={{ background: isDark ? 'hsl(180 60% 40% / 0.04)' : 'hsl(220 15% 88% / 0.3)', animationDelay: '3s' }}
        />
        <div
          className="absolute top-[35%] left-[55%] w-[30%] h-[30%] rounded-full blur-[100px] orb-pulse"
          style={{ background: isDark ? 'hsl(280 60% 50% / 0.03)' : 'hsl(220 10% 90% / 0.3)', animationDelay: '5s' }}
        />
        <div
          className="absolute top-[60%] left-[15%] w-[20%] h-[20%] rounded-full blur-[80px] orb-pulse"
          style={{ background: isDark ? 'hsl(170 80% 40% / 0.03)' : 'hsl(220 10% 92% / 0.2)', animationDelay: '7s' }}
        />
      </motion.div>

      {/* Faint glowing lines */}
      <div
        className="absolute left-0 right-0 h-px"
        style={{ top: '25%', opacity: isDark ? 0.06 : 0.04, background: isDark ? 'linear-gradient(90deg, transparent, hsl(239 84% 67% / 0.4), transparent)' : 'linear-gradient(90deg, transparent, rgba(0,0,0,0.06), transparent)' }}
      />
      <div
        className="absolute left-0 right-0 h-px"
        style={{ top: '55%', opacity: isDark ? 0.04 : 0.03, background: isDark ? 'linear-gradient(90deg, transparent, hsl(180 60% 50% / 0.3), transparent)' : 'linear-gradient(90deg, transparent, rgba(0,0,0,0.04), transparent)' }}
      />
    </div>
  );
}
