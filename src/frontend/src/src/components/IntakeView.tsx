import { useState, useRef, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { PanelGroup, Panel, PanelResizeHandle } from 'react-resizable-panels';
import ReactMarkdown from 'react-markdown';
import {
  X, Send, Loader2, CheckCircle2, AlertCircle, ChevronDown, ChevronRight,
  Plus, Trash2, Pencil, Check, Bot, User, Rocket, ArrowLeft, Sparkles,
  MessageSquare, Workflow, Zap, FileText, Target, ShieldCheck, GitMerge,
  Copy, CheckCheck,
} from 'lucide-react';
import { useQueryClient, useQuery } from '@tanstack/react-query';
import { cn } from '@/lib/utils';
import { safeFetchJson } from '@/utils/safeFetch';
import { createLogger } from '@/utils/logger';
import { logger } from '@/lib/logger';

const log = createLogger('components.IntakeView');

// ─── Plan types (re-exported from IntakeModal) ───────────────────────────────

interface PlanTask { id: string; title: string; }
interface PlanStory { id: string; title: string; acceptanceCriteria: string[]; tasks: PlanTask[]; }
interface PlanFeature { id: string; title: string; stories: PlanStory[]; }
interface PlanEpic { id: string; title: string; description: string; features: PlanFeature[]; }
interface Plan { epics: PlanEpic[]; }

interface ChatMsg { from: 'user' | 'agent'; text: string; plan?: Plan; timestamp?: string; }
interface PlanningStatus {
  stage?: string;
  provider?: string;
  model?: string;
  baseUrl?: string;
  host?: string;
  usingCodexAppServer?: boolean;
  connectionTestDurationMs?: number | null;
  connectionTestOk?: boolean | null;
  connectionTestResult?: string | null;
  requestStartedAt?: string | null;
  requestElapsedSeconds?: number | null;
  timeoutSeconds?: number | null;
  failureText?: string | null;
}

type IntakeMode = 'select' | 'simple' | 'interactive' | 'complex';
type SimplePhase = 'prompt' | 'planning' | 'chat' | 'committing' | 'committed';

// ─── Simple ID gen ────────────────────────────────────────────────────────────

let _seq = 0;
const nextId = (prefix: string) => `${prefix}-${Date.now()}-${++_seq}`;

// ─── Inline editable text ─────────────────────────────────────────────────────

function Editable({
  value, onChange, placeholder, multiline, className,
}: {
  value: string; onChange: (v: string) => void; placeholder?: string; multiline?: boolean; className?: string;
}) {
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLInputElement & HTMLTextAreaElement>(null);

  useEffect(() => { if (editing) ref.current?.select(); }, [editing]);

  if (!editing) {
    return (
      <span
        onClick={() => setEditing(true)}
        className={cn('cursor-text hover:bg-white/5 rounded px-1 -mx-1 transition-colors group', className)}
        title="Click to edit"
      >
        {value || <span className="text-muted-foreground/40">{placeholder}</span>}
        <Pencil className="w-2.5 h-2.5 inline ml-1 opacity-0 group-hover:opacity-50 text-muted-foreground" />
      </span>
    );
  }

  const props = {
    ref: ref as never,
    value,
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => onChange(e.target.value),
    onBlur: () => setEditing(false),
    onKeyDown: (e: React.KeyboardEvent) => { if (!multiline && e.key === 'Enter') setEditing(false); if (e.key === 'Escape') setEditing(false); },
    className: cn('bg-white/5 border border-primary/30 rounded px-1 text-foreground focus:outline-none w-full', className),
    autoFocus: true,
  };

  return multiline ? <textarea {...props} rows={2} /> : <input {...props} type="text" />;
}

// ─── Plan tree components ─────────────────────────────────────────────────────

function TaskRow({ task, onUpdate, onDelete }: { task: PlanTask; onUpdate: (t: PlanTask) => void; onDelete: () => void }) {
  return (
    <div className="flex items-start gap-2 pl-2 py-0.5 group">
      <span className="w-1.5 h-1.5 rounded-full bg-muted-foreground/30 mt-1.5 shrink-0" />
      <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground shrink-0">TASK</span>
      <Editable value={task.title} onChange={v => onUpdate({ ...task, title: v })} className="text-xs flex-1" placeholder="Task title" />
      <button onClick={onDelete} className="opacity-0 group-hover:opacity-100 text-destructive/60 hover:text-destructive shrink-0 transition-all">
        <Trash2 className="w-3 h-3" />
      </button>
    </div>
  );
}

function StoryRow({ story, onUpdate, onDelete }: { story: PlanStory; onUpdate: (s: PlanStory) => void; onDelete: () => void }) {
  const [open, setOpen] = useState(true);
  function updateTask(idx: number, t: PlanTask) { const tasks = [...story.tasks]; tasks[idx] = t; onUpdate({ ...story, tasks }); }
  function deleteTask(idx: number) { const tasks = story.tasks.filter((_, i) => i !== idx); onUpdate({ ...story, tasks }); }
  function addTask() { onUpdate({ ...story, tasks: [...story.tasks, { id: nextId('task'), title: 'New task' }] }); }

  return (
    <div className="border-l border-cyan-500/20 ml-2 pl-3 py-1">
      <div className="flex items-start gap-2 group">
        <button onClick={() => setOpen(o => !o)} className="mt-0.5 shrink-0">
          {open ? <ChevronDown className="w-3 h-3 text-muted-foreground" /> : <ChevronRight className="w-3 h-3 text-muted-foreground" />}
        </button>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-400 shrink-0">STORY</span>
        <Editable value={story.title} onChange={v => onUpdate({ ...story, title: v })} className="text-xs flex-1" placeholder="Story title" />
        <button onClick={onDelete} className="opacity-0 group-hover:opacity-100 text-destructive/60 hover:text-destructive shrink-0 transition-all">
          <Trash2 className="w-3 h-3" />
        </button>
      </div>
      {open && (
        <div className="mt-1 space-y-0.5">
          {story.tasks.map((task, i) => (
            <TaskRow key={task.id} task={task} onUpdate={t => updateTask(i, t)} onDelete={() => deleteTask(i)} />
          ))}
          <button onClick={addTask} className="flex items-center gap-1 text-[10px] text-muted-foreground/50 hover:text-primary/60 pl-2 py-0.5 transition-colors">
            <Plus className="w-3 h-3" /> Add task
          </button>
        </div>
      )}
    </div>
  );
}

function FeatureRow({ feature, onUpdate, onDelete }: { feature: PlanFeature; onUpdate: (f: PlanFeature) => void; onDelete: () => void }) {
  const [open, setOpen] = useState(true);
  function updateStory(idx: number, s: PlanStory) { const stories = [...feature.stories]; stories[idx] = s; onUpdate({ ...feature, stories }); }
  function deleteStory(idx: number) { const stories = feature.stories.filter((_, i) => i !== idx); onUpdate({ ...feature, stories }); }
  function addStory() {
    onUpdate({
      ...feature,
      stories: [...feature.stories, { id: nextId('story'), title: 'New story', acceptanceCriteria: [], tasks: [] }],
    });
  }

  return (
    <div className="border-l border-purple-500/20 ml-2 pl-3 py-1">
      <div className="flex items-start gap-2 group">
        <button onClick={() => setOpen(o => !o)} className="mt-0.5 shrink-0">
          {open ? <ChevronDown className="w-3 h-3 text-muted-foreground" /> : <ChevronRight className="w-3 h-3 text-muted-foreground" />}
        </button>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 shrink-0">FEAT</span>
        <Editable value={feature.title} onChange={v => onUpdate({ ...feature, title: v })} className="text-xs flex-1" placeholder="Feature title" />
        <button onClick={onDelete} className="opacity-0 group-hover:opacity-100 text-destructive/60 hover:text-destructive shrink-0 transition-all">
          <Trash2 className="w-3 h-3" />
        </button>
      </div>
      {open && (
        <div className="mt-1 space-y-1">
          {feature.stories.map((story, i) => (
            <StoryRow key={story.id} story={story} onUpdate={s => updateStory(i, s)} onDelete={() => deleteStory(i)} />
          ))}
          <button onClick={addStory} className="flex items-center gap-1 text-[10px] text-muted-foreground/50 hover:text-primary/60 pl-2 py-0.5 transition-colors">
            <Plus className="w-3 h-3" /> Add story
          </button>
        </div>
      )}
    </div>
  );
}

function EpicRow({ epic, onUpdate, onDelete }: { epic: PlanEpic; onUpdate: (e: PlanEpic) => void; onDelete: () => void }) {
  const [open, setOpen] = useState(true);
  function updateFeature(idx: number, f: PlanFeature) { const features = [...epic.features]; features[idx] = f; onUpdate({ ...epic, features }); }
  function deleteFeature(idx: number) { const features = epic.features.filter((_, i) => i !== idx); onUpdate({ ...epic, features }); }
  function addFeature() {
    onUpdate({
      ...epic,
      features: [...epic.features, { id: nextId('feat'), title: 'New feature', stories: [] }],
    });
  }

  return (
    <div className="glass-surface rounded-lg p-3 space-y-2">
      <div className="flex items-start gap-2 group">
        <button onClick={() => setOpen(o => !o)} className="mt-0.5 shrink-0">
          {open ? <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" /> : <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />}
        </button>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary shrink-0">EPIC</span>
        <Editable value={epic.title} onChange={v => onUpdate({ ...epic, title: v })} className="text-sm font-medium flex-1" placeholder="Epic title" />
        <button onClick={onDelete} className="opacity-0 group-hover:opacity-100 text-destructive/60 hover:text-destructive shrink-0 transition-all">
          <Trash2 className="w-3.5 h-3.5" />
        </button>
      </div>
      {epic.description && open && (
        <Editable value={epic.description} onChange={v => onUpdate({ ...epic, description: v })} className="text-[11px] text-muted-foreground ml-7" multiline placeholder="Description" />
      )}
      {open && (
        <div className="space-y-1 ml-1">
          {epic.features.map((f, i) => (
            <FeatureRow key={f.id} feature={f} onUpdate={feat => updateFeature(i, feat)} onDelete={() => deleteFeature(i)} />
          ))}
          <button onClick={addFeature} className="flex items-center gap-1 text-[10px] text-muted-foreground/50 hover:text-primary/60 ml-2 py-0.5 transition-colors">
            <Plus className="w-3 h-3" /> Add feature
          </button>
        </div>
      )}
    </div>
  );
}

function PlanTree({ plan, onChange }: { plan: Plan; onChange: (p: Plan) => void }) {
  function updateEpic(idx: number, e: PlanEpic) { const epics = [...plan.epics]; epics[idx] = e; onChange({ ...plan, epics }); }
  function deleteEpic(idx: number) { onChange({ ...plan, epics: plan.epics.filter((_, i) => i !== idx) }); }
  function addEpic() {
    onChange({
      ...plan,
      epics: [...plan.epics, { id: nextId('epic'), title: 'New epic', description: '', features: [] }],
    });
  }

  if (!plan.epics?.length) {
    return <div className="text-sm text-muted-foreground text-center py-8 opacity-50">No plan yet — ask the agent to break down your request.</div>;
  }

  return (
    <div className="space-y-3">
      {plan.epics.map((epic, i) => (
        <EpicRow key={epic.id} epic={epic} onUpdate={e => updateEpic(i, e)} onDelete={() => deleteEpic(i)} />
      ))}
      <button onClick={addEpic} className="flex items-center gap-1.5 text-xs text-muted-foreground/50 hover:text-primary transition-colors py-1">
        <Plus className="w-3.5 h-3.5" /> Add epic
      </button>
    </div>
  );
}

// ─── Provider icon helpers ────────────────────────────────────────────────────

const PROVIDER_META: Record<string, { label: string; color: string; icon: string }> = {
  'grok':        { label: 'Grok',      color: 'text-sky-400',     icon: '⚡' },
  'xai':         { label: 'Grok',      color: 'text-sky-400',     icon: '⚡' },
  'openai':      { label: 'OpenAI',    color: 'text-emerald-400', icon: '◉' },
  'anthropic':   { label: 'Claude',    color: 'text-amber-400',   icon: '◈' },
  'gemini':      { label: 'Gemini',    color: 'text-blue-400',    icon: '✦' },
  'google':      { label: 'Gemini',    color: 'text-blue-400',    icon: '✦' },
  'local-mesh':  { label: 'Local',     color: 'text-purple-400',  icon: '⬡' },
  'ollama':      { label: 'Ollama',    color: 'text-orange-400',  icon: '🦙' },
};

function getProviderMeta(provider?: string) {
  if (!provider) return null;
  const key = provider.toLowerCase().replace(/[^a-z-]/g, '');
  return PROVIDER_META[key] ?? { label: provider, color: 'text-muted-foreground', icon: '●' };
}

function formatModelShort(model?: string) {
  if (!model) return null;
  // Shorten verbose model names: "grok-build-0.1" → "grok-build-0.1", "gpt-4o-2024-08-06" → "gpt-4o"
  return model.replace(/-\d{4}-\d{2}-\d{2}$/, '').replace(/-preview$/, '');
}

// ─── Chat message bubble ──────────────────────────────────────────────────────

function MessageBubble({
  msg, variant = 'compact', provider, model,
}: {
  msg: ChatMsg;
  variant?: 'compact' | 'full';
  provider?: string;
  model?: string;
}) {
  const isAgent = msg.from === 'agent';
  const full = variant === 'full';
  const [copied, setCopied] = useState(false);
  const meta = isAgent ? getProviderMeta(provider) : null;
  const shortModel = formatModelShort(model);

  function handleCopy() {
    navigator.clipboard.writeText(msg.text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }

  // Provider accent color for left border (CSS variable extraction)
  const accentVar = meta?.color?.replace('text-', '') ?? 'primary';

  return (
    <motion.div
      initial={full ? { opacity: 0, y: 8 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: 'easeOut' }}
      className={cn('group/bubble flex gap-3', isAgent ? 'justify-start' : 'justify-end')}
    >
      {isAgent && (
        <div className="flex flex-col items-center gap-1.5 shrink-0 pt-1">
          <div
            className={cn(
              'rounded-xl flex items-center justify-center relative',
              full ? 'w-9 h-9' : 'w-7 h-7',
            )}
            style={{ background: 'color-mix(in srgb, currentColor 12%, transparent)' }}
          >
            {/* Ambient glow */}
            {full && <div className="absolute inset-0 rounded-xl blur-md opacity-30" style={{ background: 'currentColor' }} />}
            <span className={cn('relative z-10', meta?.color ?? 'text-primary', full ? 'text-base' : 'text-sm')}>
              {meta?.icon ?? '●'}
            </span>
          </div>
          {full && meta && (
            <span className={cn('text-[9px] font-semibold tracking-wide uppercase leading-none', meta.color)}>
              {meta.label}
            </span>
          )}
        </div>
      )}
      <div className="flex flex-col gap-1 min-w-0" style={{ maxWidth: full ? '80%' : '85%' }}>
        {/* Model badge — only on full variant for agent messages */}
        {isAgent && full && shortModel && (
          <div className="flex items-center gap-1.5 ml-1 mb-0.5">
            <span className={cn(
              'text-[10px] px-2 py-0.5 rounded-full font-medium border',
              meta?.color ?? 'text-muted-foreground',
            )} style={{ borderColor: 'color-mix(in srgb, currentColor 20%, transparent)', background: 'color-mix(in srgb, currentColor 8%, transparent)' }}>
              {shortModel}
            </span>
          </div>
        )}
        <div className={cn(
          'rounded-2xl leading-relaxed relative overflow-hidden',
          full ? 'px-5 py-4 text-sm' : 'px-3 py-2 text-xs',
          isAgent
            ? 'bg-white/[0.03] border border-white/[0.06] text-foreground/90'
            : 'bg-gradient-to-br from-primary/20 via-primary/12 to-primary/8 border border-primary/15 text-foreground',
        )}>
          {/* Left accent stripe for agent messages */}
          {isAgent && full && (
            <div
              className="absolute left-0 top-3 bottom-3 w-[2px] rounded-full"
              style={{ background: meta?.color ? `var(--tw-${accentVar}, hsl(var(--primary)))` : 'hsl(var(--primary) / 0.4)' }}
            />
          )}
          {isAgent ? (
            <div className={cn(
              'prose prose-invert prose-sm max-w-none',
              full ? 'pl-2' : '',
              '[&_p]:my-2 [&_p:first-child]:mt-0 [&_p:last-child]:mb-0',
              '[&_code]:text-[0.85em] [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded-md [&_code]:bg-white/8 [&_code]:text-emerald-300 [&_code]:font-mono [&_code]:border [&_code]:border-white/5',
              '[&_pre]:my-3 [&_pre]:rounded-xl [&_pre]:bg-black/40 [&_pre]:p-4 [&_pre]:border [&_pre]:border-white/8',
              '[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-foreground/80 [&_pre_code]:border-none',
              '[&_ul]:my-2 [&_ol]:my-2 [&_li]:my-0.5 [&_li]:leading-relaxed',
              '[&_strong]:text-foreground [&_strong]:font-semibold',
              '[&_a]:text-primary [&_a]:underline-offset-2',
              full ? '' : 'text-xs',
            )}>
              <ReactMarkdown>{msg.text}</ReactMarkdown>
            </div>
          ) : (
            <pre className="whitespace-pre-wrap font-sans leading-relaxed">{msg.text}</pre>
          )}
        </div>
        {/* Footer: copy + timestamp */}
        {isAgent && full && msg.text !== '…' && (
          <div className="flex items-center gap-2 ml-1 mt-0.5">
            <button
              onClick={handleCopy}
              className="flex items-center gap-1 text-[10px] text-muted-foreground/40 hover:text-muted-foreground/80 transition-colors"
              title="Copy response"
            >
              {copied
                ? <><CheckCheck className="w-3 h-3 text-emerald-400" /> <span className="text-emerald-400">Copied</span></>
                : <><Copy className="w-3 h-3" /> <span>Copy</span></>
              }
            </button>
            {msg.timestamp && (
              <span className="text-[10px] text-muted-foreground/25">
                {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </span>
            )}
          </div>
        )}
      </div>
      {!isAgent && (
        <div className={cn(
          'rounded-xl bg-gradient-to-br from-muted/40 to-muted/20 flex items-center justify-center shrink-0 mt-1',
          full ? 'w-9 h-9' : 'w-7 h-7',
        )}>
          <User className={cn('text-muted-foreground', full ? 'w-4.5 h-4.5' : 'w-4 h-4')} />
        </div>
      )}
    </motion.div>
  );
}

// ─── Mode selection cards ─────────────────────────────────────────────────────

const MODE_CARDS = [
  {
    id: 'simple' as IntakeMode,
    title: 'Simple',
    subtitle: 'Describe & generate',
    description: 'Describe what you want to build. The AI planner will break it down into epics, features, stories, and tasks.',
    icon: Zap,
    gradient: 'from-blue-500/20 via-blue-600/10 to-transparent',
    borderGlow: 'hover:border-blue-500/40 hover:shadow-blue-500/10',
    iconColor: 'text-blue-400',
    accentBg: 'bg-blue-500/10',
  },
  {
    id: 'interactive' as IntakeMode,
    title: 'Interactive',
    subtitle: 'Conversational planning',
    description: 'Have a back-and-forth conversation with the AI. Refine your plan iteratively through natural dialogue.',
    icon: MessageSquare,
    gradient: 'from-emerald-500/20 via-emerald-600/10 to-transparent',
    borderGlow: 'hover:border-emerald-500/40 hover:shadow-emerald-500/10',
    iconColor: 'text-emerald-400',
    accentBg: 'bg-emerald-500/10',
  },
  {
    id: 'complex' as IntakeMode,
    title: 'Complex',
    subtitle: 'Guided workflow',
    description: 'A structured, step-by-step workflow for complex initiatives with detailed constraints and architecture.',
    icon: Workflow,
    gradient: 'from-purple-500/20 via-purple-600/10 to-transparent',
    borderGlow: 'hover:border-purple-500/40 hover:shadow-purple-500/10',
    iconColor: 'text-purple-400',
    accentBg: 'bg-purple-500/10',
  },
] as const;

function ModeSelector({ onSelect }: { onSelect: (mode: IntakeMode) => void }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, y: -20 }}
      transition={{ duration: 0.4 }}
      className="flex flex-col items-center justify-center min-h-[60vh] px-6"
    >
      {/* Title block */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.1, duration: 0.5 }}
        className="text-center mb-12"
      >
        <div className="w-14 h-14 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mx-auto mb-5">
          <Sparkles className="w-7 h-7 text-primary/70" />
        </div>
        <h2 className="text-2xl font-bold text-foreground tracking-tight mb-2">
          How would you like to plan?
        </h2>
        <p className="text-sm text-muted-foreground max-w-md mx-auto">
          Choose a planning approach that fits your needs. You can always switch later.
        </p>
      </motion.div>

      {/* Mode cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 max-w-4xl w-full">
        {MODE_CARDS.map((card, i) => {
          const Icon = card.icon;
          return (
            <motion.button
              key={card.id}
              initial={{ opacity: 0, y: 30 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.15 + i * 0.08, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
              onClick={() => onSelect(card.id)}
              className={cn(
                'group relative text-left rounded-xl overflow-hidden',
                'border border-white/8 bg-white/[0.03]',
                'p-6 transition-all duration-300 ease-out',
                'hover:-translate-y-1 hover:shadow-2xl',
                card.borderGlow,
              )}
            >
              {/* Background gradient */}
              <div className={cn(
                'absolute inset-0 bg-gradient-to-br opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none',
                card.gradient,
              )} />

              {/* Shimmer line */}
              <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-500" />

              <div className="relative z-10">
                <div className={cn(
                  'w-11 h-11 rounded-xl flex items-center justify-center mb-4 transition-colors duration-300',
                  card.accentBg,
                  'group-hover:scale-110 transform transition-transform duration-300',
                )}>
                  <Icon className={cn('w-5 h-5', card.iconColor)} />
                </div>

                <h3 className="text-base font-semibold text-foreground mb-0.5 group-hover:text-white transition-colors">
                  {card.title}
                </h3>
                <p className={cn('text-[11px] font-medium mb-3', card.iconColor, 'opacity-70')}>
                  {card.subtitle}
                </p>
                <p className="text-xs text-muted-foreground leading-relaxed group-hover:text-muted-foreground/80 transition-colors">
                  {card.description}
                </p>
              </div>
            </motion.button>
          );
        })}
      </div>
    </motion.div>
  );
}

// ─── Complex mode placeholder ─────────────────────────────────────────────────

const WORKFLOW_STEPS = [
  { icon: Target, label: 'Define Scope', description: 'Outline objectives, constraints, and success criteria' },
  { icon: FileText, label: 'Set Requirements', description: 'Specify technical and business requirements' },
  { icon: GitMerge, label: 'Generate Architecture', description: 'AI produces an architectural design proposal' },
  { icon: ShieldCheck, label: 'Review & Approve', description: 'Validate the plan with team and stakeholders' },
  { icon: Rocket, label: 'Commit & Deploy', description: 'Push approved work to the task queue for execution' },
];

function ComplexModePlaceholder({ onBack }: { onBack: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3 }}
      className="flex flex-col items-center justify-center min-h-[60vh] px-6"
    >
      <div className="max-w-lg w-full">
        {/* Header */}
        <div className="text-center mb-10">
          <div className="w-14 h-14 rounded-2xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center mx-auto mb-5">
            <Workflow className="w-7 h-7 text-purple-400" />
          </div>
          <h2 className="text-xl font-bold text-foreground tracking-tight mb-2">
            Guided Workflow
          </h2>
          <p className="text-sm text-muted-foreground">
            A structured approach for complex initiatives. Coming soon.
          </p>
        </div>

        {/* Steps */}
        <div className="space-y-0">
          {WORKFLOW_STEPS.map((step, i) => {
            const Icon = step.icon;
            const isLast = i === WORKFLOW_STEPS.length - 1;
            return (
              <motion.div
                key={step.label}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 0.4, x: 0 }}
                transition={{ delay: 0.1 + i * 0.07, duration: 0.4 }}
                className="flex items-start gap-4"
              >
                {/* Connector */}
                <div className="flex flex-col items-center">
                  <div className="w-9 h-9 rounded-lg bg-purple-500/8 border border-purple-500/15 flex items-center justify-center shrink-0">
                    <Icon className="w-4 h-4 text-purple-400/60" />
                  </div>
                  {!isLast && (
                    <div className="w-px h-8 bg-gradient-to-b from-purple-500/15 to-transparent mt-1" />
                  )}
                </div>
                <div className="pt-1.5 pb-4">
                  <span className="text-sm font-medium text-foreground/60">{step.label}</span>
                  <p className="text-[11px] text-muted-foreground/50 mt-0.5">{step.description}</p>
                </div>
              </motion.div>
            );
          })}
        </div>

        {/* CTA */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.6 }}
          className="text-center mt-8"
        >
          <p className="text-xs text-muted-foreground/50 mb-4">
            Use <strong className="text-foreground/50">Simple</strong> or <strong className="text-foreground/50">Interactive</strong> mode in the meantime.
          </p>
          <button
            onClick={onBack}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-muted-foreground hover:text-foreground hover:bg-white/10 transition-all"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            Choose another mode
          </button>
        </motion.div>
      </div>
    </motion.div>
  );
}

// ─── Interactive chat mode ────────────────────────────────────────────────────

function InteractiveMode({
  projectId,
  projectName,
  onBack,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onBack: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [plan, setPlan] = useState<Plan>({ epics: [] });
  const [planSource, setPlanSource] = useState<'llm' | 'placeholder' | null>(null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const [committed, setCommitted] = useState(false);
  const [commitCount, setCommitCount] = useState(0);
  const [committing, setCommitting] = useState(false);
  const [showPlan, setShowPlan] = useState(false);
  const [planEstimate, setPlanEstimate] = useState<any>(null);
  const [activeProvider, setActiveProvider] = useState<string | undefined>();
  const [activeModel, setActiveModel] = useState<string | undefined>();
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Focus input on mount
  useEffect(() => {
    setTimeout(() => inputRef.current?.focus(), 100);
  }, []);

  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || sending) return;

    setInput('');
    setSending(true);
    setError('');

    const userMsg: ChatMsg = { from: 'user', text };
    setMessages(prev => [...prev, userMsg]);

    // Thinking indicator
    const thinkingMsg: ChatMsg = { from: 'agent', text: '…' };
    setMessages(prev => [...prev, thinkingMsg]);

    try {
      let sid = sessionId;

      if (!sid) {
        // First message — start a new session
        const data = await safeFetchJson<Record<string, any>>('/api/intake/session', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: projectId, prompt: text }),
        });
        sid = data.sessionId;
        setSessionId(sid);
        log.info('sendMessage', 'Interactive session started', { sessionId: sid });

        // Poll until ready
        let ready = false;
        let attempts = 0;
        while (!ready && attempts < 120) {
          await new Promise(r => setTimeout(r, 2500));
          try {
            const poll = await safeFetchJson<Record<string, any>>(`/api/intake/session/${sid}`);
            if (poll.status === 'ready' || poll.status === 'failed' || poll.planningStatus?.stage === 'ready' || poll.planningStatus?.stage === 'failed') {
              ready = true;
              setMessages(prev => prev.filter(m => m !== thinkingMsg));
              const serverMsgs = (poll.messages ?? []).filter((m: ChatMsg) => m.from === 'user' || m.from === 'agent');
              // Backend may not persist the initial user prompt in session.Messages —
              // ensure the user's original message is always visible by prepending it
              // if the server response doesn't include a user message.
              const hasUserMsg = serverMsgs.some((m: ChatMsg) => m.from === 'user');
              if (hasUserMsg) {
                setMessages(serverMsgs);
              } else {
                setMessages([userMsg, ...serverMsgs]);
              }
              setPlan(poll.plan ?? { epics: [] });
              setPlanSource(poll.planSource === 'placeholder' || poll.planSource === 'llm' ? poll.planSource : null);
              if (poll.planEstimate) setPlanEstimate(poll.planEstimate);
              if (poll.plan?.epics?.length > 0) setShowPlan(true);
              // Extract provider/model from planning status
              if (poll.planningStatus?.provider) setActiveProvider(poll.planningStatus.provider);
              if (poll.planningStatus?.model) setActiveModel(poll.planningStatus.model);
            }
          } catch {
            // retry
          }
          attempts++;
        }
        if (!ready) {
          setMessages(prev => prev.filter(m => m !== thinkingMsg));
          setError('Planning timed out. Try again.');
        }
      } else {
        // Follow-up message
        const data = await safeFetchJson<Record<string, any>>(`/api/intake/session/${sid}/message`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, plan }),
        });
        setMessages(prev => prev.filter(m => m !== thinkingMsg));
        setMessages(data.messages ?? []);
        if (data.plan?.epics?.length) {
          setPlan(data.plan);
          setShowPlan(true);
        }
        if (data.planSource === 'placeholder' || data.planSource === 'llm') setPlanSource(data.planSource);
        if (data.planEstimate) setPlanEstimate(data.planEstimate);
        // Update provider/model on refinement too
        if (data.planningStatus?.provider) setActiveProvider(data.planningStatus.provider);
        if (data.planningStatus?.model) setActiveModel(data.planningStatus.model);
      }
    } catch (e: unknown) {
      log.error('sendMessage', 'Interactive message failed', { error: String(e) });
      setMessages(prev => prev.filter(m => m !== thinkingMsg));
      setError(e instanceof Error ? e.message : 'Failed to get response');
    } finally {
      setSending(false);
    }
  }, [input, sending, sessionId, projectId, plan]);

  async function commitWork() {
    if (committed || committing || !sessionId) return;
    setCommitting(true);
    setError('');
    try {
      const data = await safeFetchJson<Record<string, any>>(`/api/intake/session/${sessionId}/commit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      });
      setCommitCount(data.count ?? 0);
      setCommitted(true);
      log.info('commitWork', 'Interactive work committed', { sessionId, taskCount: data.count ?? 0 });
      logger.agentReasoning('intake', `Committed plan with ${data.count ?? 0} tasks via Interactive mode`, {
        component: 'IntakeView',
        sessionId,
        projectId,
      });
      qc.invalidateQueries({ queryKey: ['snapshot'] });
      qc.invalidateQueries({ queryKey: ['project-tasks', projectId] });
    } catch (e: unknown) {
      log.error('commitWork', 'Interactive commit failed', { error: String(e) });
      setError(e instanceof Error ? e.message : 'Failed to commit');
    } finally {
      setCommitting(false);
    }
  }

  const epicCount = plan.epics?.length ?? 0;
  const hasPlan = epicCount > 0;

  if (committed) {
    return (
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="flex flex-col items-center justify-center min-h-[50vh] gap-4 text-center"
      >
        <div className="w-16 h-16 rounded-full bg-success/15 flex items-center justify-center">
          <CheckCircle2 className="w-8 h-8 text-success" />
        </div>
        <div>
          <h2 className="text-lg font-semibold text-foreground mb-1">Work committed!</h2>
          <p className="text-sm text-muted-foreground">
            {commitCount} task{commitCount !== 1 ? 's' : ''} added to the queue.
          </p>
        </div>
        <button
          onClick={onClose}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary/15 border border-primary/20 text-primary text-sm font-medium hover:bg-primary/25 transition-colors"
        >
          <Check className="w-4 h-4" /> Done
        </button>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3 }}
      className="flex flex-col h-[calc(100vh-220px)] min-h-[400px]"
    >
      {/* Header bar */}
      <div className="flex items-center justify-between px-1 pb-4 shrink-0">
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="p-1.5 text-muted-foreground/50 hover:text-muted-foreground rounded-lg hover:bg-white/5 transition-all"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-xl bg-emerald-500/15 border border-emerald-500/20 flex items-center justify-center">
              <MessageSquare className="w-4 h-4 text-emerald-400" />
            </div>
            <div className="flex items-center gap-2.5">
              <div>
                <span className="text-sm font-semibold text-foreground">Interactive Planning</span>
                <span className="text-xs text-muted-foreground/60 ml-2">→ {projectName}</span>
              </div>
              {activeModel && (() => {
                const meta = getProviderMeta(activeProvider);
                return (
                  <span className={cn(
                    'text-[10px] px-2.5 py-1 rounded-full border font-medium flex items-center gap-1.5',
                    meta ? meta.color : 'text-muted-foreground',
                  )} style={{ borderColor: 'color-mix(in srgb, currentColor 20%, transparent)', background: 'color-mix(in srgb, currentColor 8%, transparent)' }}>
                    {meta && <span className="text-xs">{meta.icon}</span>}
                    {formatModelShort(activeModel)}
                  </span>
                );
              })()}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {hasPlan && (
            <button
              onClick={() => setShowPlan(s => !s)}
              className={cn(
                'flex items-center gap-1.5 text-[11px] px-3.5 py-2 rounded-xl border font-medium transition-all',
                showPlan
                  ? 'bg-primary/15 border-primary/25 text-primary shadow-sm shadow-primary/10'
                  : 'bg-white/5 border-white/10 text-muted-foreground hover:text-foreground hover:bg-white/10',
              )}
            >
              <FileText className="w-3.5 h-3.5" />
              {showPlan ? 'Hide Plan' : 'Show Plan'}
            </button>
          )}
          {hasPlan && (
            <button
              onClick={commitWork}
              disabled={committing}
              className="flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-success/15 border border-success/20 text-success text-[11px] font-semibold hover:bg-success/25 hover:shadow-sm hover:shadow-success/10 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
            >
              {committing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
              {committing ? 'Committing…' : 'Commit Work'}
            </button>
          )}
        </div>
      </div>

      {/* Gradient separator */}
      <div className="h-px bg-gradient-to-r from-transparent via-white/10 to-transparent mb-0" />

      {/* Content: chat + optional plan panel */}
      <div className="flex-1 flex overflow-hidden rounded-2xl border border-white/[0.06] bg-black/20 mt-4 shadow-xl shadow-black/20">
        {/* Chat area */}
        <div className={cn('flex flex-col', showPlan ? 'w-[55%]' : 'w-full')}>
          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {messages.length === 0 && !sending && (
              <div className="flex flex-col items-center justify-center h-full text-center py-16">
                <div className="w-14 h-14 rounded-2xl bg-emerald-500/10 border border-emerald-500/15 flex items-center justify-center mb-5">
                  <MessageSquare className="w-7 h-7 text-emerald-400/50" />
                </div>
                <h3 className="text-sm font-medium text-foreground/70 mb-1">Start a conversation</h3>
                <p className="text-xs text-muted-foreground/40 max-w-sm leading-relaxed">
                  Describe what you want to build. The AI will help you plan it through conversation.
                </p>
              </div>
            )}
            {messages.filter(m => m.from === 'user' || m.from === 'agent').map((msg, i) => (
              <MessageBubble key={i} msg={msg} variant="full" provider={activeProvider} model={activeModel} />
            ))}
            {sending && messages[messages.length - 1]?.text === '…' && (
              <div className="flex items-center gap-3 pl-12">
                <div className="flex items-center gap-1.5 px-4 py-2.5 rounded-2xl bg-white/[0.03] border border-white/[0.06]">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400/60 animate-bounce [animation-delay:0ms]" />
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400/60 animate-bounce [animation-delay:150ms]" />
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400/60 animate-bounce [animation-delay:300ms]" />
                </div>
              </div>
            )}
            <div ref={chatBottomRef} />
          </div>

          {/* Error */}
          {error && (
            <div className="mx-5 mb-2 flex items-center gap-2 text-destructive text-xs p-3 rounded-xl bg-destructive/10 border border-destructive/20">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              {error}
            </div>
          )}

          {/* Input bar — floating glass pill */}
          <div className="p-5 pt-2">
            <div className="relative rounded-2xl bg-white/[0.04] border border-white/[0.08] shadow-lg shadow-black/20 transition-all duration-300 focus-within:border-emerald-500/30 focus-within:shadow-emerald-500/5 focus-within:bg-white/[0.05]">
              <div className="flex gap-3 items-end p-3">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
                  placeholder={sessionId ? 'Refine the plan…' : 'What do you want to build?'}
                  rows={1}
                  className="flex-1 bg-transparent px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/30 focus:outline-none resize-none"
                  disabled={sending}
                  style={{ minHeight: '36px', maxHeight: '120px' }}
                  onInput={(e) => {
                    const el = e.currentTarget;
                    el.style.height = '36px';
                    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
                  }}
                />
                <button
                  onClick={sendMessage}
                  disabled={!input.trim() || sending}
                  className={cn(
                    'p-2.5 rounded-xl transition-all shrink-0',
                    input.trim() && !sending
                      ? 'bg-emerald-500/20 border border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/30 shadow-sm shadow-emerald-500/10'
                      : 'bg-white/5 border border-white/8 text-muted-foreground/30 cursor-not-allowed',
                  )}
                >
                  {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                </button>
              </div>
              <div className="flex items-center justify-between px-5 pb-2">
                <p className="text-[10px] text-muted-foreground/20">
                  Enter to send · Shift+Enter for newline
                </p>
                {activeModel && (
                  <p className="text-[10px] text-muted-foreground/20">
                    Powered by {formatModelShort(activeModel)}
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Plan panel (toggle) */}
        <AnimatePresence>
          {showPlan && (
            <motion.div
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: '45%', opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: 'easeInOut' }}
              className="border-l border-white/[0.06] flex flex-col overflow-hidden"
            >
              <div className="px-5 py-3.5 border-b border-white/[0.06] flex items-center justify-between shrink-0 bg-white/[0.02]">
                <div className="flex items-center gap-2">
                  <FileText className="w-3.5 h-3.5 text-primary/50" />
                  <span className="text-xs font-semibold text-foreground">Work Breakdown</span>
                </div>
                <span className="text-[10px] text-muted-foreground/40">Click to edit</span>
              </div>
              {planSource === 'placeholder' && (
                <div className="px-5 py-2 text-[11px] text-amber-200/90 bg-amber-500/10 border-b border-amber-500/20 shrink-0">
                  Placeholder breakdown — not from AI
                </div>
              )}
              {planEstimate && planEstimate.warning && (
                <div className={cn(
                  "px-5 py-2 text-[11px] border-b shrink-0 flex items-center gap-2",
                  (planEstimate.leaves ?? 0) > (planEstimate.hardCap ?? 12)
                    ? "text-destructive bg-destructive/10 border-destructive/30"
                    : "text-amber-200/90 bg-amber-500/10 border-amber-500/20"
                )}>
                  <span className="font-medium">⚠ {planEstimate.warning}</span>
                </div>
              )}
              <div className="flex-1 overflow-y-auto p-5">
                <PlanTree plan={plan} onChange={setPlan} />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

// ─── Simple mode (refactored from IntakeModal) ────────────────────────────────

function SimpleMode({
  projectId,
  projectName,
  onBack,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onBack: () => void;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [phase, setPhase] = useState<SimplePhase>('prompt');
  const [prompt, setPrompt] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [plan, setPlan] = useState<Plan>({ epics: [] });
  const [planSource, setPlanSource] = useState<'llm' | 'placeholder' | null>(null);
  const [chatInput, setChatInput] = useState('');
  const [planningStatus, setPlanningStatus] = useState<PlanningStatus | null>(null);
  const [error, setError] = useState('');
  const [commitCount, setCommitCount] = useState(0);
  const [committed, setCommitted] = useState(false);
  const [planEstimate, setPlanEstimate] = useState<any>(null);
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const [liveNow, setLiveNow] = useState(() => Date.now());

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const { data: pollData, error: pollError } = useQuery({
    queryKey: ['intake-session', sessionId],
    enabled: !!(sessionId && phase === 'planning'),
    queryFn: () => safeFetchJson(`/api/intake/session/${sessionId}`),
    refetchInterval: 5000,
  });

  useEffect(() => {
    if (pollData && phase === 'planning') {
      setPlanningStatus(pollData.planningStatus ?? null);
      setMessages(pollData.messages ?? []);
      setPlan(pollData.plan ?? { epics: [] });
      setPlanSource(pollData.planSource === 'placeholder' || pollData.planSource === 'llm' ? pollData.planSource : null);
      if (pollData.planEstimate) setPlanEstimate(pollData.planEstimate);
      if (pollData.status === 'ready' || pollData.status === 'failed') setPhase('chat');
    }
  }, [pollData, phase]);

  useEffect(() => {
    if (pollError && phase === 'planning') {
      setError(pollError instanceof Error ? pollError.message : 'Failed to load planning status');
      setPhase('prompt');
    }
  }, [pollError, phase]);

  useEffect(() => {
    const isActivelyPlanning = phase === 'planning' && planningStatus?.stage === 'requesting_plan' && !!planningStatus?.requestStartedAt;
    if (!isActivelyPlanning) return;
    const id = setInterval(() => setLiveNow(Date.now()), 200);
    return () => clearInterval(id);
  }, [phase, planningStatus?.stage, planningStatus?.requestStartedAt]);

  async function startSession() {
    if (!prompt.trim()) return;
    setPhase('planning');
    setError('');
    try {
      const data = await safeFetchJson<Record<string, any>>('/api/intake/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo: projectId, prompt: prompt.trim() }),
      });
      log.info('startSession', 'Simple planning session started', { sessionId: data.sessionId, projectId });
      setSessionId(data.sessionId);
      setMessages(data.messages ?? []);
      setPlan(data.plan ?? { epics: [] });
      setPlanningStatus(data.planningStatus ?? null);
      setPlanSource(data.planSource === 'placeholder' || data.planSource === 'llm' ? data.planSource : null);
      if (data.planEstimate) setPlanEstimate(data.planEstimate);
      setPhase(data.status === 'ready' || data.status === 'failed' ? 'chat' : 'planning');
    } catch (e: unknown) {
      log.error('startSession', 'Simple planning failed', { projectId, error: String(e) });
      setError(e instanceof Error ? e.message : 'Failed to connect to planner');
      setPhase('prompt');
    }
  }

  async function sendMessage() {
    if (!chatInput.trim() || phase !== 'chat') return;
    const text = chatInput.trim();
    setChatInput('');
    const optimisticMsg: ChatMsg = { from: 'user', text };
    setMessages(prev => [...prev, optimisticMsg]);
    const thinkingMsg: ChatMsg = { from: 'agent', text: '…thinking…' };
    setMessages(prev => [...prev, thinkingMsg]);
    setError('');
    try {
      const data = await safeFetchJson<Record<string, any>>(`/api/intake/session/${sessionId}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, plan }),
      });
      setMessages(data.messages ?? []);
      if (data.plan?.epics?.length) setPlan(data.plan);
      if (data.planSource === 'placeholder' || data.planSource === 'llm') setPlanSource(data.planSource);
      setPlanningStatus(data.planningStatus ?? null);
      if (data.planEstimate) setPlanEstimate(data.planEstimate);
    } catch (e: unknown) {
      log.error('sendMessage', 'Simple chat failed', { sessionId, error: String(e) });
      setMessages(prev => prev.filter(m => m !== thinkingMsg));
      setError(e instanceof Error ? e.message : 'Failed to get response');
    }
  }

  async function commitWork() {
    if (committed || phase === 'committing') return;
    setPhase('committing');
    setError('');
    try {
      const data = await safeFetchJson<Record<string, any>>(`/api/intake/session/${sessionId}/commit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      });
      setCommitCount(data.count ?? 0);
      setCommitted(true);
      setPhase('committed');
      log.info('commitWork', 'Simple work committed', { sessionId, taskCount: data.count ?? 0 });
      logger.agentReasoning('intake', `Committed plan with ${data.count ?? 0} tasks via Simple mode`, {
        component: 'IntakeView',
        sessionId,
        projectId,
      });
      qc.invalidateQueries({ queryKey: ['snapshot'] });
      qc.invalidateQueries({ queryKey: ['project-tasks', projectId] });
    } catch (e: unknown) {
      log.error('commitWork', 'Simple commit failed', { sessionId, error: String(e) });
      setError(e instanceof Error ? e.message : 'Failed to commit');
      setPhase('chat');
    }
  }

  const connectionElapsed = planningStatus?.connectionTestDurationMs != null
    ? `${planningStatus.connectionTestDurationMs.toFixed(0)} ms`
    : null;
  const plannerStageLabel = planningStatus?.stage === 'testing_connection'
    ? 'Testing remote LLM connection…'
    : planningStatus?.stage === 'requesting_plan'
      ? 'Waiting for planner response…'
      : planningStatus?.stage === 'failed'
        ? 'Planner request failed'
        : 'Preparing planner…';

  const liveElapsedSeconds = (() => {
    const serverVal = planningStatus?.requestElapsedSeconds ?? 0;
    if (!planningStatus?.requestStartedAt || planningStatus.stage !== 'requesting_plan') return serverVal;
    try {
      const startMs = new Date(planningStatus.requestStartedAt).getTime();
      if (isNaN(startMs)) return serverVal;
      const live = Math.max(0, (liveNow - startMs) / 1000);
      return Math.max(live, serverVal);
    } catch { return serverVal; }
  })();

  const displayHost = (() => {
    const h = planningStatus?.host;
    if (h && h.trim()) return h.trim();
    const b = planningStatus?.baseUrl;
    if (b && b.trim()) {
      try { const u = new URL(b.trim()); return u.hostname + (u.port ? `:${u.port}` : ''); } catch { return b.trim(); }
    }
    return '—';
  })();

  const epicCount = plan.epics?.length ?? 0;
  const taskCount = plan.epics?.reduce((a, ep) =>
    a + (ep.features?.reduce((b, f) =>
      b + (f.stories?.reduce((c, s) => c + (s.tasks?.length ?? 0), 0) ?? 0), 0) ?? 0), 0) ?? 0;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3 }}
      className="flex flex-col"
    >
      {/* Back button + header */}
      <div className="flex items-center gap-3 mb-5">
        <button
          onClick={onBack}
          className="p-1.5 text-muted-foreground/50 hover:text-muted-foreground rounded-lg hover:bg-white/5 transition-all"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-lg bg-blue-500/15 flex items-center justify-center">
            <Zap className="w-3.5 h-3.5 text-blue-400" />
          </div>
          <div>
            <span className="text-sm font-semibold text-foreground">Simple Planning</span>
            <span className="text-xs text-muted-foreground ml-2">→ {projectName}</span>
          </div>
        </div>
        {phase === 'chat' && (planEstimate && planEstimate.leaves != null ? planEstimate.leaves > 0 : epicCount > 0) && (
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 border border-primary/20 text-primary">
            {planEstimate && planEstimate.leaves != null
              ? `${planEstimate.leaves} leaf${planEstimate.leaves !== 1 ? 's' : ''}${planEstimate.fastpath ? ' (fastpath)' : ''}`
              : `${epicCount} epic${epicCount !== 1 ? 's' : ''} · ${taskCount} task${taskCount !== 1 ? 's' : ''}`}
          </span>
        )}
        {phase === 'chat' && planSource === 'placeholder' && (
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/35 text-amber-200">
            Placeholder breakdown (not from AI)
          </span>
        )}
      </div>

      {/* ── Initial prompt ── */}
      {(phase === 'prompt' || phase === 'planning') && (
        <div className="flex flex-col items-center justify-center min-h-[50vh] gap-6">
          <div className="text-center max-w-lg">
            <div className="w-14 h-14 rounded-2xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center mx-auto mb-4">
              <Bot className="w-7 h-7 text-blue-400" />
            </div>
            <h2 className="text-lg font-semibold text-foreground mb-2">What do you want to build?</h2>
            <p className="text-sm text-muted-foreground">
              Describe your request in plain language. The planning agent will break it down into epics, features, stories, and tasks.
            </p>
          </div>
          <div className="w-full max-w-xl space-y-3">
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); startSession(); } }}
              placeholder="e.g. Add a user authentication system with email/password login, password reset, and session management…"
              rows={5}
              className="w-full rounded-xl bg-white/5 border border-white/10 px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:border-blue-500/40 resize-none transition-colors"
              disabled={phase === 'planning'}
              autoFocus
            />
            {phase === 'planning' && !planningStatus && (
              <div className="rounded-xl border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-foreground/90 space-y-2">
                <div className="flex items-center gap-2 text-primary">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span className="font-medium">Starting planning session…</span>
                </div>
                <div className="text-[11px] text-muted-foreground">Waiting for planner status from the server.</div>
              </div>
            )}
            {phase === 'planning' && planningStatus && (
              <div className="rounded-xl border border-primary/20 bg-primary/5 px-4 py-3 text-xs text-foreground/90 space-y-2">
                <div className="flex items-center gap-2 text-primary">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span className="font-medium">{plannerStageLabel}</span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                  <div><span className="text-foreground/80">Provider:</span> {planningStatus.provider ?? '—'} {planningStatus.model ? `· ${planningStatus.model}` : ''}</div>
                  <div><span className="text-foreground/80">Host:</span> {displayHost}</div>
                  <div><span className="text-foreground/80">Connection test:</span> {planningStatus.connectionTestOk == null ? 'pending' : planningStatus.connectionTestOk ? 'ok' : 'failed'}{connectionElapsed ? ` · ${connectionElapsed}` : ''}</div>
                </div>
                <div className="pt-1">
                  <div className="flex justify-between text-[11px] mb-1">
                    <span className="text-foreground/80">Planning Progress</span>
                    <span className="text-muted-foreground">
                      {Math.floor(liveElapsedSeconds)}s / {planningStatus.timeoutSeconds ?? 120}s
                    </span>
                  </div>
                  <div className="h-1.5 w-full bg-black/40 rounded-full overflow-hidden border border-white/5">
                    <div
                      className={cn("h-full transition-all duration-300", planningStatus.stage === 'failed' ? "bg-destructive" : "bg-primary")}
                      style={{ width: `${Math.min((liveElapsedSeconds / (planningStatus.timeoutSeconds ?? 120)) * 100, 100)}%` }}
                    />
                  </div>
                </div>
                {planningStatus.connectionTestResult && (
                  <div className="text-[11px] text-muted-foreground break-all">{planningStatus.connectionTestResult}</div>
                )}
                {planningStatus.failureText && (
                  <div className="text-[11px] text-destructive break-all bg-destructive/10 p-2 rounded border border-destructive/20 mt-2">
                    {planningStatus.failureText.includes('timeout') || (liveElapsedSeconds >= (planningStatus.timeoutSeconds ?? 120))
                      ? `Planning timed out after ${planningStatus.timeoutSeconds ?? 120} seconds. The model took too long to respond.`
                      : planningStatus.failureText}
                  </div>
                )}
              </div>
            )}
            {error && (
              <div className="flex items-center gap-2 text-destructive text-xs">
                <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                {error}
              </div>
            )}
            <button
              onClick={startSession}
              disabled={!prompt.trim() || phase === 'planning'}
              className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {phase === 'planning' ? (
                <><Loader2 className="w-4 h-4 animate-spin" /> Planning…</>
              ) : (
                <><Bot className="w-4 h-4" /> Start Planning</>
              )}
            </button>
            <p className="text-[10px] text-muted-foreground/40 text-center">Enter to submit · Shift+Enter for newline</p>
          </div>
        </div>
      )}

      {/* ── Chat + plan split ── */}
      {(phase === 'chat' || phase === 'committing') && (
        <div className="flex overflow-hidden rounded-xl border border-white/8 bg-black/20" style={{ height: 'calc(100vh - 280px)', minHeight: '400px' }}>
          {/* Chat panel */}
          <div className="w-[40%] min-w-[280px] flex flex-col border-r border-white/8">
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              {messages.filter(m => m.from === 'user' || m.from === 'agent').map((msg, i) => (
                <MessageBubble key={i} msg={msg} />
              ))}
              {phase === 'committing' && (
                <div className="flex items-center gap-2 text-muted-foreground text-xs">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Committing work to the queue…
                </div>
              )}
              <div ref={chatBottomRef} />
            </div>
            {phase === 'chat' && (
              <div className="border-t border-white/8 p-3 space-y-2">
                {error && (
                  <div className="flex flex-col gap-2 p-2.5 bg-destructive/10 border border-destructive/20 rounded-lg">
                    <div className="flex items-center gap-1.5 text-destructive text-[11px]">
                      <AlertCircle className="w-3.5 h-3.5 shrink-0" />{error}
                    </div>
                    <div className="flex gap-2">
                      <button onClick={startSession} className="text-[10px] px-3 py-1.5 bg-destructive text-destructive-foreground hover:bg-destructive/90 rounded-md font-medium transition-colors">
                        Retry Planning
                      </button>
                    </div>
                  </div>
                )}
                {!error && planSource === 'placeholder' && (
                  <div className="flex flex-col gap-2 p-2.5 bg-amber-500/10 border border-amber-500/20 rounded-lg">
                    <div className="flex items-center gap-1.5 text-amber-500 text-[11px]">
                      <AlertCircle className="w-3.5 h-3.5 shrink-0" />The model failed to generate a plan.
                    </div>
                    <div className="flex gap-2">
                      <button onClick={startSession} className="text-[10px] px-3 py-1.5 bg-amber-500 text-amber-950 hover:bg-amber-400 rounded-md font-medium transition-colors">
                        Retry Planning
                      </button>
                    </div>
                  </div>
                )}
                <div className="flex gap-2">
                  <textarea
                    value={chatInput}
                    onChange={e => setChatInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
                    placeholder="Refine the plan… (Enter to send)"
                    rows={2}
                    className="flex-1 rounded-lg bg-white/5 border border-white/8 px-3 py-2 text-xs text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:border-primary/30 resize-none transition-colors"
                  />
                  <button
                    onClick={sendMessage}
                    disabled={!chatInput.trim()}
                    className="px-3 rounded-lg bg-primary/15 border border-primary/20 text-primary hover:bg-primary/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
                  >
                    <Send className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Plan tree panel */}
          <div className="flex-1 flex flex-col">
            <div className="px-4 py-2.5 border-b border-white/8 flex items-center justify-between shrink-0">
              <span className="text-xs font-semibold text-foreground">Work Breakdown</span>
              <span className="text-[10px] text-muted-foreground">Click any item to edit</span>
            </div>
            {planSource === 'placeholder' && (
              <div className="px-4 py-2 text-[11px] text-amber-200/90 bg-amber-500/10 border-b border-amber-500/20 shrink-0">
                This tree is a <strong>placeholder template</strong> — not from the model.
              </div>
            )}
            {planEstimate && planEstimate.warning && (
              <div className={cn(
                "px-4 py-1.5 text-[11px] border-b shrink-0 flex items-center gap-2",
                (planEstimate.leaves ?? 0) > (planEstimate.hardCap ?? 12)
                  ? "text-destructive bg-destructive/10 border-destructive/30"
                  : "text-amber-200/90 bg-amber-500/10 border-amber-500/20"
              )}>
                <span className="font-medium">⚠ {planEstimate.warning}</span>
                <span className="text-[10px] opacity-70">· leaves: {planEstimate.leaves ?? 0} / cap {planEstimate.hardCap ?? 12}</span>
              </div>
            )}
            <div className="flex-1 overflow-y-auto p-4">
              <PlanTree plan={plan} onChange={setPlan} />
            </div>
            <div className="border-t border-white/8 p-3 flex items-center justify-between bg-white/[0.02] shrink-0">
              <span className="text-[11px] text-muted-foreground">
                {planEstimate && planEstimate.leaves != null
                  ? `${planEstimate.leaves} leaf${planEstimate.leaves !== 1 ? 's' : ''} (est, cap ${planEstimate.hardCap ?? 12}${planEstimate.fastpath ? ', fastpath' : ''})`
                  : (epicCount > 0 ? `${epicCount} epic${epicCount !== 1 ? 's' : ''} · ${taskCount} task${taskCount !== 1 ? 's' : ''}` : 'No work items yet')}
              </span>
              <button
                onClick={commitWork}
                disabled={epicCount === 0 || phase === 'committing' || committed}
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-success/15 border border-success/20 text-success text-xs font-medium hover:bg-success/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {phase === 'committing' ? (
                  <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Committing…</>
                ) : (
                  <><Rocket className="w-3.5 h-3.5" /> Commit Work</>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Committed ── */}
      {phase === 'committed' && (
        <div className="flex flex-col items-center justify-center min-h-[50vh] gap-4 text-center">
          <div className="w-16 h-16 rounded-full bg-success/15 flex items-center justify-center">
            <CheckCircle2 className="w-8 h-8 text-success" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-foreground mb-1">Work committed!</h2>
            <p className="text-sm text-muted-foreground">
              {commitCount} task{commitCount !== 1 ? 's' : ''} added to the queue and ready for agents to pick up.
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => {
                setPhase('prompt');
                setPrompt('');
                setSessionId('');
                setMessages([]);
                setPlan({ epics: [] });
                setPlanSource(null);
                setCommitted(false);
              }}
              className="px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
            >
              Plan more work
            </button>
            <button
              onClick={onClose}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary/15 border border-primary/20 text-primary text-sm font-medium hover:bg-primary/25 transition-colors"
            >
              <Check className="w-4 h-4" /> Done
            </button>
          </div>
        </div>
      )}
    </motion.div>
  );
}

// ─── Main IntakeView component ────────────────────────────────────────────────

interface IntakeViewProps {
  projectId: string;
  projectName: string;
  onClose: () => void;
}

export function IntakeView({ projectId, projectName, onClose }: IntakeViewProps) {
  const [mode, setMode] = useState<IntakeMode>('select');

  function handleBack() {
    setMode('select');
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
      className="relative"
    >
      {/* Close / back to project button */}
      {mode === 'select' && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.2 }}
          className="flex items-center justify-between mb-2"
        >
          <button
            onClick={onClose}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors group"
          >
            <ArrowLeft className="w-3.5 h-3.5 group-hover:-translate-x-0.5 transition-transform" />
            Back to project
          </button>
        </motion.div>
      )}

      <AnimatePresence mode="wait">
        {mode === 'select' && (
          <ModeSelector key="select" onSelect={setMode} />
        )}
        {mode === 'simple' && (
          <SimpleMode
            key="simple"
            projectId={projectId}
            projectName={projectName}
            onBack={handleBack}
            onClose={onClose}
          />
        )}
        {mode === 'interactive' && (
          <InteractiveMode
            key="interactive"
            projectId={projectId}
            projectName={projectName}
            onBack={handleBack}
            onClose={onClose}
          />
        )}
        {mode === 'complex' && (
          <ComplexModePlaceholder key="complex" onBack={handleBack} />
        )}
      </AnimatePresence>
    </motion.div>
  );
}
