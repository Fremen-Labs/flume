import { motion } from 'framer-motion';
import { Sliders } from 'lucide-react';

interface Props {
  frontierLocalRatio: number;
  complexityThreshold: number;
  onRatioChange: (ratio: number) => void;
  onComplexityChange: (threshold: number) => void;
}

const COMPLEXITY_LABELS: Record<number, string> = {
  1: 'Trivial',
  2: 'Simple',
  3: 'Evaluation',
  4: 'Generic',
  5: 'Moderate',
  6: 'Implementation',
  7: 'Complex',
  8: 'Planning',
  9: 'Advanced',
  10: 'Critical',
};

export function HybridTuner({ frontierLocalRatio, complexityThreshold, onRatioChange, onComplexityChange }: Props) {
  const localPct = Math.round((1 - frontierLocalRatio) * 100);
  const frontierPct = Math.round(frontierLocalRatio * 100);

  return (
    <div className="glass-card p-5 space-y-5" id="hybrid-tuner">
      {/* Section header */}
      <div className="flex items-center gap-2">
        <div className="w-8 h-8 rounded-lg bg-violet-500/15 flex items-center justify-center">
          <Sliders className="w-4 h-4 text-violet-400" />
        </div>
        <div>
          <p className="text-sm font-semibold text-foreground">Hybrid Tuning</p>
          <p className="text-[10px] text-muted-foreground/70">Balance local and frontier routing</p>
        </div>
      </div>

      {/* Frontier/Local Ratio Slider */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-emerald-400 font-medium">Local: {localPct}%</span>
          <span className="text-violet-400 font-medium">Frontier: {frontierPct}%</span>
        </div>

        {/* Gradient track behind slider */}
        <div className="relative">
          <div className="absolute inset-0 h-2 rounded-full mt-[5px]"
            style={{
              background: 'linear-gradient(to right, rgb(52, 211, 153) 0%, rgb(99, 102, 241) 50%, rgb(139, 92, 246) 100%)',
              opacity: 0.25,
            }}
          />
          <input
            type="range"
            min={0}
            max={100}
            value={frontierPct}
            onChange={(e) => onRatioChange(Number(e.target.value) / 100)}
            className="relative w-full h-2 rounded-full appearance-none bg-transparent cursor-pointer z-10 accent-violet-500"
            id="hybrid-ratio-slider"
          />
        </div>
      </div>

      {/* Complexity Threshold Slider */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">Complexity Threshold</span>
          <span className="font-mono text-foreground">
            {complexityThreshold} — {COMPLEXITY_LABELS[complexityThreshold] ?? 'Custom'}
          </span>
        </div>

        <input
          type="range"
          min={1}
          max={10}
          value={complexityThreshold}
          onChange={(e) => onComplexityChange(Number(e.target.value))}
          className="w-full h-1.5 rounded-full appearance-none bg-white/10 accent-indigo-500 cursor-pointer"
          id="complexity-threshold-slider"
        />

        {/* Reference marks */}
        <div className="flex justify-between text-[9px] text-muted-foreground/50 px-0.5">
          <span>Evaluation (3)</span>
          <span>Code (6)</span>
          <span>Planning (8)</span>
        </div>
      </div>

      {/* Description text */}
      <motion.p
        key={`${frontierPct}-${complexityThreshold}`}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="text-xs text-muted-foreground/80 leading-relaxed bg-white/[0.03] rounded-lg px-3 py-2"
      >
        ~{localPct}% of standard requests will use local nodes. All tasks with complexity ≥ {complexityThreshold} (
        {COMPLEXITY_LABELS[complexityThreshold]?.toLowerCase() ?? 'custom'} and above) will always use frontier models.
      </motion.p>
    </div>
  );
}
