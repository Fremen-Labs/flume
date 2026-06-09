import { motion } from 'framer-motion';
import { useMouseParallax } from '@/hooks/useMouseParallax';
import { useTheme } from '@/hooks/useTheme';

export function MeshBackground() {
  const bgOffset = useMouseParallax(0.01);
  const { skin } = useTheme();
  const isRetro = skin === 'retro';

  const gradientBg = isRetro
    ? `
        radial-gradient(ellipse 120% 80% at 15% 8%, hsl(276 45% 8%) 0%, transparent 55%),
        radial-gradient(ellipse 100% 70% at 85% 85%, hsl(33 60% 6%) 0%, transparent 50%),
        radial-gradient(ellipse 70% 50% at 50% 50%, hsl(240 40% 5%) 0%, transparent 45%),
        hsl(240 33% 6%)
      `
    : `
        radial-gradient(ellipse 120% 80% at 20% 10%, hsl(230 60% 8%) 0%, transparent 60%),
        radial-gradient(ellipse 100% 60% at 80% 90%, hsl(200 50% 6%) 0%, transparent 50%),
        radial-gradient(ellipse 80% 40% at 50% 50%, hsl(260 30% 5%) 0%, transparent 40%),
        hsl(240 10% 2%)
      `;

  const orbPrimary = isRetro ? 'hsl(276 88% 53% / 0.07)' : 'hsl(239 84% 67% / 0.05)';
  const orbSecondary = isRetro ? 'hsl(33 100% 50% / 0.06)' : 'hsl(180 60% 40% / 0.04)';
  const orbTertiary = isRetro ? 'hsl(170 80% 42% / 0.05)' : 'hsl(280 60% 50% / 0.03)';
  const orbQuaternary = isRetro ? 'hsl(51 100% 50% / 0.04)' : 'hsl(170 80% 40% / 0.03)';
  const lineGradient = isRetro
    ? `linear-gradient(90deg, transparent, hsl(33 100% 50% / 0.35), transparent)`
    : 'linear-gradient(90deg, transparent, hsl(239 84% 67% / 0.4), transparent)';

  const gridSize = isRetro ? '20px 20px' : '60px 60px';
  const gridOpacity = isRetro ? 0.08 : 0.03;

  return (
    <div className="fixed inset-0 overflow-hidden pointer-events-none z-0">
      <div className="absolute inset-0 transition-colors duration-500" style={{ background: gradientBg }} />

      <div
        className="absolute inset-0"
        style={{
          opacity: gridOpacity,
          backgroundImage: `
            linear-gradient(var(--grid-line-color, rgba(255,255,255,0.05)) 1px, transparent 1px),
            linear-gradient(90deg, var(--grid-line-color, rgba(255,255,255,0.05)) 1px, transparent 1px)
          `,
          backgroundSize: gridSize,
        }}
      />

      {/* CRT scanline overlay for Retro skin */}
      {isRetro && (
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            background: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.15) 2px, rgba(0,0,0,0.15) 4px)',
            opacity: 0.5,
          }}
        />
      )}

      <motion.div
        className="absolute inset-0"
        animate={{ x: bgOffset.x, y: bgOffset.y }}
        transition={{ type: 'spring', stiffness: 40, damping: 30 }}
      >
        <div className="absolute top-[-15%] left-[-5%] w-[55%] h-[55%] rounded-full blur-[140px] orb-pulse" style={{ background: orbPrimary }} />
        <div className="absolute bottom-[-10%] right-[-5%] w-[45%] h-[45%] rounded-full blur-[120px] orb-pulse" style={{ background: orbSecondary, animationDelay: '3s' }} />
        <div className="absolute top-[35%] left-[55%] w-[30%] h-[30%] rounded-full blur-[100px] orb-pulse" style={{ background: orbTertiary, animationDelay: '5s' }} />
        <div className="absolute top-[60%] left-[15%] w-[20%] h-[20%] rounded-full blur-[80px] orb-pulse" style={{ background: orbQuaternary, animationDelay: '7s' }} />
      </motion.div>

      <div className="absolute left-0 right-0 h-px" style={{ top: '25%', opacity: 0.06, background: lineGradient }} />
      <div
        className="absolute left-0 right-0 h-px"
        style={{
          top: '55%',
          opacity: 0.04,
          background: isRetro ? 'linear-gradient(90deg, transparent, hsl(276 88% 53% / 0.22), transparent)' : 'linear-gradient(90deg, transparent, hsl(180 60% 50% / 0.3), transparent)',
        }}
      />
    </div>
  );
}
