import { useState, useRef, useEffect, useMemo } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { PanelGroup, Panel, PanelResizeHandle } from 'react-resizable-panels';
import {
  X, Send, Loader2, CheckCircle2, AlertCircle, ChevronDown, ChevronRight,
  Plus, Trash2, Pencil, Check, Bot, User, Rocket,
} from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { cn } from '@/lib/utils';
import type { LlmSettingsResponse } from '@/types';

async function fetchLlmSettingsBrief(): Promise<LlmSettingsResponse> {
  const res = await fetch('/api/settings/llm');
  if (!res.ok) throw new Error(`Settings fetch failed: ${res.status}`);
  return res.json();
}

// ─── Plan types ───────────────────────────────────────────────────────────────

interface PlanTask { id: string; title: string; }
interface PlanStory { id: string; title: string; acceptanceCriteria: string[]; tasks: PlanTask[]; }
interface PlanFeature { id: string; title: string; stories: PlanStory[]; }
interface PlanEpic { id: string; title: string; description: string; features: PlanFeature[]; }
interface Plan { epics: PlanEpic[]; }

interface ChatMsg { from: 'user' | 'agent'; text: string; plan?: Plan; }

type Phase = 'prompt' | 'planning' | 'chat' | 'committing' | 'committed';

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

// ─── Plan tree ────────────────────────────────────────────────────────────────

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

  function updateTask(idx: number, t: PlanTask) {
    const tasks = [...story.tasks]; tasks[idx] = t; onUpdate({ ...story, tasks });
  }
  function deleteTask(idx: number) {
    const tasks = story.tasks.filter((_, i) => i !== idx); onUpdate({ ...story, tasks });
  }
  function addTask() {
    onUpdate({ ...story, tasks: [...story.tasks, { id: nextId('task'), title: 'New task' }] });
  }

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

  function updateStory(idx: number, s: PlanStory) {
    const stories = [...feature.stories]; stories[idx] = s; onUpdate({ ...feature, stories });
  }
  function deleteStory(idx: number) {
    const stories = feature.stories.filter((_, i) => i !== idx); onUpdate({ ...feature, stories });
  }
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

  function updateFeature(idx: number, f: PlanFeature) {
    const features = [...epic.features]; features[idx] = f; onUpdate({ ...epic, features });
  }
  function deleteFeature(idx: number) {
    const features = epic.features.filter((_, i) => i !== idx); onUpdate({ ...epic, features });
  }
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
  function updateEpic(idx: number, e: PlanEpic) {
    const epics = [...plan.epics]; epics[idx] = e; onChange({ ...plan, epics });
  }
  function deleteEpic(idx: number) {
    onChange({ ...plan, epics: plan.epics.filter((_, i) => i !== idx) });
  }
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

// ─── Chat message bubble ──────────────────────────────────────────────────────

function MessageBubble({ msg }: { msg: ChatMsg }) {
  const isAgent = msg.from === 'agent';
  return (
    <div className={cn('flex gap-2.5', isAgent ? 'justify-start' : 'justify-end')}>
      {isAgent && (
        <div className="w-7 h-7 rounded-full bg-primary/15 flex items-center justify-center shrink-0 mt-0.5">
          <Bot className="w-4 h-4 text-primary" />
        </div>
      )}
      <div className={cn(
        'max-w-[85%] rounded-xl px-3 py-2 text-xs leading-relaxed',
        isAgent ? 'bg-white/5 border border-white/8 text-foreground/90' : 'bg-primary/15 border border-primary/20 text-foreground',
      )}>
        <pre className="whitespace-pre-wrap font-sans">{msg.text}</pre>
      </div>
      {!isAgent && (
        <div className="w-7 h-7 rounded-full bg-muted/30 flex items-center justify-center shrink-0 mt-0.5">
          <User className="w-4 h-4 text-muted-foreground" />
        </div>
      )}
    </div>
  );
}

// ─── Main modal ───────────────────────────────────────────────────────────────

interface IntakeModalProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  projectId: string;
  projectName: string;
}

export function IntakeModal({ open, onOpenChange, projectId, projectName }: IntakeModalProps) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [phase, setPhase] = useState<Phase>('prompt');
  const [prompt, setPrompt] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [plan, setPlan] = useState<Plan>({ epics: [] });
  /** 'placeholder' = skeleton from server when LLM failed or returned no plan; 'llm' = model-produced tree */
  const [planSource, setPlanSource] = useState<'llm' | 'placeholder' | null>(null);
  const [chatInput, setChatInput] = useState('');
  const [error, setError] = useState('');
  const [commitCount, setCommitCount] = useState(0);
  const [committed, setCommitted] = useState(false);
  const chatBottomRef = useRef<HTMLDivElement>(null);

  const { data: llmSettings } = useQuery({
    queryKey: ['settings', 'llm'],
    queryFn: fetchLlmSettingsBrief,
    staleTime: 30_000,
    enabled: open,
  });

  /** OpenAI + OAuth (typical Codex token) cannot call /v1/chat/completions — Plan New Work needs sk- or another provider. */
  const plannerLikelyBlockedByOAuth = useMemo(() => {
    const s = llmSettings?.settings;
    if (!s || s.provider !== 'openai') return false;
    if (s.authMode !== 'oauth') return false;
    const st = llmSettings.oauthStatus;
    if (!st?.hasAccessToken) return true;
    if (st.accessTokenJwtParsed && st.hasModelRequestScope === true) return false;
    return true;
  }, [llmSettings]);

  // Reset when opened
  useEffect(() => {
    if (open) {
      setPhase('prompt');
      setPrompt('');
      setSessionId('');
      setMessages([]);
      setPlan({ epics: [] });
      setPlanSource(null);
      setChatInput('');
      setError('');
      setCommitCount(0);
      setCommitted(false);
    }
  }, [open]);

  // Scroll chat to bottom
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  async function startSession() {
    if (!prompt.trim()) return;
    setPhase('planning');
    setError('');
    try {
      const res = await fetch('/api/intake/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo: projectId, prompt: prompt.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Failed to start session');
      setSessionId(data.sessionId);
      setMessages(data.messages ?? []);
      setPlan(data.plan ?? { epics: [] });
      setPlanSource(
        data.planSource === 'placeholder' || data.planSource === 'llm' ? data.planSource : null,
      );
      setPhase('chat');
    } catch (e: unknown) {
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

    // Add thinking indicator
    const thinkingMsg: ChatMsg = { from: 'agent', text: '…thinking…' };
    setMessages(prev => [...prev, thinkingMsg]);

    setError('');
    try {
      const res = await fetch(`/api/intake/session/${sessionId}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, plan }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Failed to send message');
      setMessages(data.messages ?? []);
      if (data.plan?.epics?.length) setPlan(data.plan);
      if (data.planSource === 'placeholder' || data.planSource === 'llm') {
        setPlanSource(data.planSource);
      }
    } catch (e: unknown) {
      // Remove thinking msg and show error
      setMessages(prev => prev.filter(m => m !== thinkingMsg));
      setError(e instanceof Error ? e.message : 'Failed to get response');
    }
  }

  async function commitWork() {
    if (committed || phase === 'committing') return;
    setPhase('committing');
    setError('');
    try {
      const res = await fetch(`/api/intake/session/${sessionId}/commit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? 'Failed to commit plan');
      setCommitCount(data.count ?? 0);
      setCommitted(true);
      setPhase('committed');
      qc.invalidateQueries({ queryKey: ['snapshot'] });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to commit');
      setPhase('chat');
    }
  }

  const epicCount = plan.epics?.length ?? 0;
  const taskCount = plan.epics?.reduce((a, ep) =>
    a + ep.features?.reduce((b, f) =>
      b + f.stories?.reduce((c, s) => c + (s.tasks?.length ?? 0), 0), 0) ?? 0, 0) ?? 0;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-[50%] top-[50%] z-50 translate-x-[-50%] translate-y-[-50%]',
            'w-[calc(100vw-32px)] h-[calc(100vh-48px)] max-w-[1200px]',
            'bg-[#0a0f1a] border border-white/10 rounded-xl shadow-2xl overflow-hidden',
            'data-[state=open]:animate-in data-[state=closed]:animate-out',
            'data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
            'data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
            'flex flex-col',
          )}
        >
          <DialogPrimitive.Title className="sr-only">Plan New Work — {projectName}</DialogPrimitive.Title>

          {/* Header */}
          <div className="flex items-center justify-between px-5 py-3 border-b border-white/8 bg-white/[0.02] shrink-0">
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-primary/15 flex items-center justify-center">
                <Bot className="w-4 h-4 text-primary" />
              </div>
              <div>
                <span className="text-sm font-semibold text-foreground">Plan New Work</span>
                <span className="text-xs text-muted-foreground ml-2">→ {projectName}</span>
              </div>
              {phase === 'chat' && epicCount > 0 && (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 border border-primary/20 text-primary">
                  {epicCount} epic{epicCount !== 1 ? 's' : ''} · {taskCount} task{taskCount !== 1 ? 's' : ''}
                </span>
              )}
              {phase === 'chat' && planSource === 'placeholder' && (
                <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/15 border border-amber-500/35 text-amber-200">
                  Placeholder breakdown (not from AI)
                </span>
              )}
            </div>
            <DialogPrimitive.Close className="rounded-md p-1.5 text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors">
              <X className="w-4 h-4" />
            </DialogPrimitive.Close>
          </div>

          {plannerLikelyBlockedByOAuth && (
            <div className="px-5 py-2.5 border-b border-amber-500/30 bg-amber-500/10 text-[11px] text-amber-100 leading-snug shrink-0">
              <strong className="text-amber-50">OpenAI is set to OAuth (Codex / ChatGPT).</strong> Plan New Work calls{' '}
              <code className="text-[10px] opacity-90">/v1/chat/completions</code>, which requires{' '}
              <code className="text-[10px] opacity-90">model.request</code> — that scope is{' '}
              <strong>not</strong> available on Codex browser OAuth. Use a <strong>platform API key</strong> (
              <code className="text-[10px] opacity-90">sk-…</code>) in{' '}
              <button
                type="button"
                className="underline font-semibold text-amber-50 hover:text-white"
                onClick={() => {
                  onOpenChange(false);
                  navigate('/settings');
                }}
              >
                Settings → LLM
              </button>
              , then <code className="text-[10px] opacity-90">./flume restart --all</code>. Codex OAuth is still fine for
              the Codex CLI / app-server path.
            </div>
          )}

          {/* Body */}
          <div className="flex-1 overflow-hidden">

            {/* ── Initial prompt ── */}
            {(phase === 'prompt' || phase === 'planning') && (
              <div className="flex flex-col items-center justify-center h-full gap-6 px-6">
                <div className="text-center max-w-lg">
                  <div className="w-14 h-14 rounded-2xl bg-primary/15 flex items-center justify-center mx-auto mb-4">
                    <Bot className="w-7 h-7 text-primary" />
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
                    onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) startSession(); }}
                    placeholder="e.g. Add a user authentication system with email/password login, password reset, and session management…"
                    rows={5}
                    className="w-full rounded-xl bg-white/5 border border-white/10 px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:border-primary/40 resize-none transition-colors"
                    disabled={phase === 'planning'}
                    autoFocus
                  />
                  {error && (
                    <div className="flex items-start gap-2 text-destructive text-xs">
                      <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                      <span className="whitespace-pre-wrap break-words">{error}</span>
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
                  <p className="text-[10px] text-muted-foreground/40 text-center">Ctrl+Enter to submit</p>
                </div>
              </div>
            )}

            {/* ── Chat + plan ── */}
            {(phase === 'chat' || phase === 'committing') && (
              <PanelGroup direction="horizontal" className="h-full">
                {/* Chat panel */}
                <Panel defaultSize={40} minSize={28}>
                  <div className="h-full flex flex-col border-r border-white/8">
                    {/* Messages */}
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

                    {/* Input */}
                    {phase === 'chat' && (
                      <div className="border-t border-white/8 p-3 space-y-2">
                        {error && (
                          <div className="flex items-start gap-1.5 text-destructive text-[11px]">
                            <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
                            <span className="whitespace-pre-wrap break-words">{error}</span>
                          </div>
                        )}
                        <div className="flex gap-2">
                          <textarea
                            value={chatInput}
                            onChange={e => setChatInput(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }}
                            placeholder="Refine the plan… (Enter to send, Shift+Enter for newline)"
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
                </Panel>

                <PanelResizeHandle className="w-[3px] bg-white/5 hover:bg-primary/30 transition-colors cursor-col-resize" />

                {/* Plan tree panel */}
                <Panel minSize={35}>
                  <div className="h-full flex flex-col">
                    <div className="px-4 py-2.5 border-b border-white/8 flex items-center justify-between shrink-0">
                      <span className="text-xs font-semibold text-foreground">Work Breakdown</span>
                      <span className="text-[10px] text-muted-foreground">Click any item to edit</span>
                    </div>
                    {planSource === 'placeholder' && (
                      <div className="px-4 py-2 text-[11px] text-amber-200/90 bg-amber-500/10 border-b border-amber-500/20 shrink-0">
                        This tree is a <strong>placeholder template</strong> (your epic title comes from the first line of
                        your prompt). It is <strong>not</strong> from the model until the planner runs successfully — fix
                        LLM settings, then start a new plan or refine in chat.
                      </div>
                    )}

                    <div className="flex-1 overflow-y-auto p-4">
                      <PlanTree plan={plan} onChange={setPlan} />
                    </div>

                    {/* Commit bar */}
                    <div className="border-t border-white/8 p-3 flex items-center justify-between bg-white/[0.02] shrink-0">
                      <span className="text-[11px] text-muted-foreground">
                        {epicCount > 0 ? `${epicCount} epic${epicCount !== 1 ? 's' : ''} · ${taskCount} task${taskCount !== 1 ? 's' : ''}` : 'No work items yet'}
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
                </Panel>
              </PanelGroup>
            )}

            {/* ── Committed ── */}
            {phase === 'committed' && (
              <div className="flex flex-col items-center justify-center h-full gap-4 text-center">
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
                    onClick={() => onOpenChange(false)}
                    className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary/15 border border-primary/20 text-primary text-sm font-medium hover:bg-primary/25 transition-colors"
                  >
                    <Check className="w-4 h-4" /> Done
                  </button>
                </div>
              </div>
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
