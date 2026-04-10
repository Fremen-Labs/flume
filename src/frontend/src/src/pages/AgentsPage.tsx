import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Bot,
  Loader2,
  AlertCircle,
  ChevronDown,
  CheckCircle2,
  XCircle,
  Zap,
  Clock,
  Brain,
  Code2,
  TestTube2,
  ShieldCheck,
  BookOpen,
  PenLine,
  Server,
  Cpu,
  Save,
  RotateCcw,
} from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type {
  AgentModelsCredentialGroup,
  AgentModelsResponse,
  AgentModelsRoleEffective,
} from '@/types';

import agentAvatar1 from '@/assets/agents/agent-1.png';
import agentAvatar2 from '@/assets/agents/agent-2.png';
import agentAvatar3 from '@/assets/agents/agent-3.png';
import agentAvatar4 from '@/assets/agents/agent-4.png';

// ─── Constants ─────────────────────────────────────────────────────────────

const SETTINGS_DEFAULT_CREDENTIAL_ID = '__settings_default__';
const OLLAMA_CREDENTIAL_ID = '__ollama__';
const OPENAI_OAUTH_CREDENTIAL_ID = '__openai_oauth__';
const USE_GLOBAL_DEFAULT = '__global__';

const agentAvatars = [agentAvatar1, agentAvatar2, agentAvatar3, agentAvatar4];

interface RoleInfo {
  label: string;
  description: string;
  icon: React.ElementType;
  color: string;
}

const ROLE_INFO: Record<string, RoleInfo> = {
  intake: {
    label: 'Intake Agent',
    description: 'Parses raw requests, clarifies requirements, and routes work to the planner.',
    icon: BookOpen,
    color: 'text-blue-400',
  },
  pm: {
    label: 'Project Manager',
    description: 'Decomposes epics into stories and tasks; manages the work backlog.',
    icon: PenLine,
    color: 'text-violet-400',
  },
  planner: {
    label: 'Planner',
    description: 'Drafts implementation strategies before code is written.',
    icon: Brain,
    color: 'text-indigo-400',
  },
  implementer: {
    label: 'Implementer',
    description: 'Writes, edits, and commits code changes using AST and file tools.',
    icon: Code2,
    color: 'text-emerald-400',
  },
  tester: {
    label: 'Tester',
    description: 'Generates and runs tests to validate implementer output.',
    icon: TestTube2,
    color: 'text-amber-400',
  },
  reviewer: {
    label: 'Code Reviewer',
    description: 'Reviews diffs for quality, correctness, and style compliance.',
    icon: ShieldCheck,
    color: 'text-rose-400',
  },
  architect: {
    label: 'Architect',
    description: 'Designs system structure and evaluates technical decisions.',
    icon: Server,
    color: 'text-cyan-400',
  },
  devops: {
    label: 'DevOps',
    description: 'Handles CI/CD, infrastructure changes, and deployment tasks.',
    icon: Cpu,
    color: 'text-orange-400',
  },
  qa: {
    label: 'QA Engineer',
    description: 'Performs end-to-end quality assurance across completed stories.',
    icon: CheckCircle2,
    color: 'text-teal-400',
  },
  'memory-updater': {
    label: 'Memory Updater',
    description: 'Syncs agent learnings and context back to the knowledge base.',
    icon: BookOpen,
    color: 'text-pink-400',
  },
};

function getRoleInfo(roleId: string): RoleInfo {
  return (
    ROLE_INFO[roleId] ?? {
      label: roleId,
      description: 'Custom agent role.',
      icon: Bot,
      color: 'text-muted-foreground',
    }
  );
}

// ─── Types ──────────────────────────────────────────────────────────────────

type RoleForm = {
  useGlobal: boolean;
  credentialId: string;
  provider: string;
  model: string;
  executionHost: string;
};

type SaveState = 'idle' | 'saving' | 'success' | 'error';

// ─── Utilities ───────────────────────────────────────────────────────────────

function timeAgo(ts?: string) {
  if (!ts) return 'unknown';
  const diff = Date.now() - new Date(ts).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m ago`;
}

function elapsed(ts?: string) {
  if (!ts) return null;
  const diff = Date.now() - new Date(ts).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function fmtTokens(n?: number) {
  if (!n) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

async function fetchAgentModels(): Promise<AgentModelsResponse> {
  const res = await fetch('/api/settings/agent-models');
  if (!res.ok) throw new Error(`agent-models: ${res.status}`);
  return res.json();
}

function normalizeRoleSpec(
  raw: unknown,
  fallback: { credentialId: string; provider: string; model: string; host: string },
): RoleForm {
  if (typeof raw === 'string') {
    return { useGlobal: false, credentialId: fallback.credentialId, provider: fallback.provider, model: raw || fallback.model, executionHost: fallback.host };
  }
  if (raw && typeof raw === 'object') {
    const o = raw as AgentModelsRoleEffective;
    return {
      useGlobal: false,
      credentialId: o.credentialId || fallback.credentialId,
      provider: o.provider || fallback.provider,
      model: o.model || fallback.model,
      executionHost: o.executionHost || fallback.host,
    };
  }
  return { useGlobal: true, credentialId: SETTINGS_DEFAULT_CREDENTIAL_ID, provider: fallback.provider, model: fallback.model, executionHost: fallback.host };
}

// ─── RoleModelPicker ─────────────────────────────────────────────────────────

interface RoleModelPickerProps {
  form: RoleForm;
  cfg: AgentModelsResponse;
  onChange: (patch: Partial<RoleForm>) => void;
}

function RoleModelPicker({ form, cfg, onChange }: RoleModelPickerProps) {
  const credentials: AgentModelsCredentialGroup[] = cfg.availableCredentials ?? [];
  const readyCredentials = credentials.filter((c) => c.configured);
  const hasCredentials = readyCredentials.length > 0;

  // Credential-aware path (when saved API keys exist)
  if (hasCredentials) {
    const activeCred = readyCredentials.find((c) => c.credentialId === form.credentialId) ?? readyCredentials[0];
    const models = activeCred?.models ?? [];
    const allowCustom = activeCred?.allowCustomModelId ?? true;

    return (
      <>
        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">Credential / Key</Label>
          <Select
            value={form.credentialId || readyCredentials[0]?.credentialId}
            onValueChange={(v) => {
              const cred = readyCredentials.find((c) => c.credentialId === v);
              onChange({ credentialId: v, provider: cred?.providerId ?? form.provider, model: cred?.models?.[0]?.id ?? form.model });
            }}
          >
            <SelectTrigger className="h-9 text-xs">
              <SelectValue placeholder="Select credential" />
            </SelectTrigger>
            <SelectContent>
              {readyCredentials.map((c) => (
                <SelectItem key={c.credentialId} value={c.credentialId} className="text-xs">
                  {c.shortLabel ?? c.label}
                  {c.keySuffix ? ` · ···${c.keySuffix}` : ''}
                  {c.credentialId === SETTINGS_DEFAULT_CREDENTIAL_ID ? ' (Settings default)' : ''}
                  {c.credentialId === OLLAMA_CREDENTIAL_ID ? ' (local)' : ''}
                  {c.credentialId === OPENAI_OAUTH_CREDENTIAL_ID ? ' (OAuth)' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">Model</Label>
          {allowCustom || models.length === 0 ? (
            <Input
              className="h-9 text-xs font-mono"
              value={form.model}
              onChange={(e) => onChange({ model: e.target.value })}
              placeholder="e.g. qwen2.5-coder:7b"
            />
          ) : (
            <Select value={form.model} onValueChange={(v) => onChange({ model: v })}>
              <SelectTrigger className="h-9 text-xs">
                <SelectValue placeholder="Select model" />
              </SelectTrigger>
              <SelectContent>
                {models.map((m) => (
                  <SelectItem key={m.id} value={m.id} className="text-xs font-mono">
                    {m.name || m.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>
      </>
    );
  }

  // Provider-only path (Ollama / no saved keys)
  const providers = (cfg.availableProviders ?? []).filter((p) => p.configured);
  const activeProvider = providers.find((p) => p.providerId === form.provider) ?? providers[0];
  const models = activeProvider?.models ?? [];
  const allowCustom = activeProvider?.allowCustomModelId ?? true;

  return (
    <>
      <div className="space-y-1.5">
        <Label className="text-xs text-muted-foreground">Provider</Label>
        <Select
          value={form.provider || providers[0]?.providerId}
          onValueChange={(v) => {
            const p = providers.find((x) => x.providerId === v);
            onChange({ provider: v, model: p?.models?.[0]?.id ?? '' });
          }}
        >
          <SelectTrigger className="h-9 text-xs">
            <SelectValue placeholder="Select provider" />
          </SelectTrigger>
          <SelectContent>
            {providers.map((p) => (
              <SelectItem key={p.providerId} value={p.providerId} className="text-xs capitalize">
                {p.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label className="text-xs text-muted-foreground">Model</Label>
        {allowCustom || models.length === 0 ? (
          <Input
            className="h-9 text-xs font-mono"
            value={form.model}
            onChange={(e) => onChange({ model: e.target.value })}
            placeholder="e.g. qwen2.5-coder:7b"
          />
        ) : (
          <Select value={form.model} onValueChange={(v) => onChange({ model: v })}>
            <SelectTrigger className="h-9 text-xs">
              <SelectValue placeholder="Select model" />
            </SelectTrigger>
            <SelectContent>
              {models.map((m) => (
                <SelectItem key={m.id} value={m.id} className="text-xs font-mono">
                  {m.name || m.id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
    </>
  );
}

// ─── Inline Role Config Panel ─────────────────────────────────────────────────

interface RoleConfigPanelProps {
  roleId: string;
  form: RoleForm;
  cfg: AgentModelsResponse;
  onChange: (patch: Partial<RoleForm>) => void;
  onSave: () => Promise<void>;
  onReset: () => void;
  saveState: SaveState;
  saveMsg: string;
}

function RoleConfigPanel({ roleId, form, cfg, onChange, onSave, onReset, saveState, saveMsg }: RoleConfigPanelProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.22, ease: 'easeInOut' }}
      className="overflow-hidden"
    >
      <div className="mt-3 pt-3 border-t border-border/30 space-y-3">
        {/* Use global default toggle */}
        <label className="flex items-center gap-2.5 cursor-pointer group">
          <div
            role="checkbox"
            aria-checked={form.useGlobal}
            tabIndex={0}
            className={`w-8 h-4 rounded-full transition-colors flex items-center px-0.5 ${
              form.useGlobal ? 'bg-primary' : 'bg-muted-foreground/30'
            }`}
            onClick={() => onChange({ useGlobal: !form.useGlobal })}
            onKeyDown={(e) => e.key === ' ' && onChange({ useGlobal: !form.useGlobal })}
          >
            <div
              className={`w-3 h-3 rounded-full bg-white shadow transition-transform ${
                form.useGlobal ? 'translate-x-4' : 'translate-x-0'
              }`}
            />
          </div>
          <span className="text-xs text-muted-foreground group-hover:text-foreground transition-colors">
            Use global default <span className="text-primary/70">(Settings → LLM)</span>
          </span>
        </label>

        {!form.useGlobal && (
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
            <RoleModelPicker form={form} cfg={cfg} onChange={onChange} />
            <div className="space-y-1.5">
              <Label className="text-xs text-muted-foreground">Execution host</Label>
              <Input
                className="h-9 text-xs font-mono"
                value={form.executionHost}
                onChange={(e) => onChange({ executionHost: e.target.value })}
                placeholder={cfg.defaultExecutionHost || 'e.g. 127.0.0.1'}
              />
            </div>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 pt-1">
          <div className="flex items-center gap-2 min-w-0">
            {saveState === 'success' && (
              <span className="flex items-center gap-1 text-xs text-emerald-500">
                <CheckCircle2 className="w-3.5 h-3.5" /> Saved — workers pick this up on next task cycle.
              </span>
            )}
            {saveState === 'error' && (
              <span className="flex items-center gap-1 text-xs text-destructive">
                <XCircle className="w-3.5 h-3.5" /> {saveMsg}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={onReset}
            >
              <RotateCcw className="w-3 h-3" />
              Reset
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-8 gap-1.5 text-xs"
              onClick={() => void onSave()}
              disabled={saveState === 'saving'}
            >
              {saveState === 'saving' ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Save className="w-3.5 h-3.5" />
              )}
              Save role
            </Button>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

// ─── Worker Card ──────────────────────────────────────────────────────────────

interface WorkerCardProps {
  worker: NonNullable<ReturnType<typeof useSnapshot>['data']>['workers'][0];
  index: number;
  cfg: AgentModelsResponse | undefined;
  form: RoleForm | undefined;
  onChange: (patch: Partial<RoleForm>) => void;
  onSave: (roleId: string) => Promise<void>;
  onReset: (roleId: string) => void;
  saveState: SaveState;
  saveMsg: string;
}

function WorkerCard({ worker, index, cfg, form, onChange, onSave, onReset, saveState, saveMsg }: WorkerCardProps) {
  const [configOpen, setConfigOpen] = useState(false);
  const isActive = worker.status === 'claimed' || worker.status === 'active';
  const prov = worker.llm_provider ?? worker.preferred_llm_provider;
  const roleId = worker.role;
  const info = getRoleInfo(roleId);
  const RoleIcon = info.icon;
  const totalTokens = (worker.input_tokens ?? 0) + (worker.output_tokens ?? 0);
  const elapsedTime = isActive && worker.task_started_at ? elapsed(worker.task_started_at) : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.03 }}
      className={`glass-card-sweep border transition-all duration-500 ease-out p-5 relative overflow-hidden
        ${isActive
          ? 'border-primary/30 hover:border-primary/50 shadow-lg shadow-primary/5'
          : 'border-white/10 hover:border-white/20'
        }`}
    >
      {/* Active pulse ring */}
      {isActive && (
        <div className="absolute inset-0 rounded-[inherit] pointer-events-none">
          <div className="absolute inset-0 rounded-[inherit] border border-primary/20 animate-pulse" />
        </div>
      )}

      <div className="relative z-10">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-3">
            <div className="relative">
              <div className={`w-10 h-10 rounded-full overflow-hidden ring-2 transition-all ${isActive ? 'ring-primary/40' : 'ring-white/10'}`}>
                <img src={agentAvatars[index % agentAvatars.length]} alt={worker.name} className="w-full h-full object-cover" />
              </div>
              {isActive && (
                <div className="absolute -bottom-0.5 -right-0.5 w-3 h-3 bg-primary rounded-full border-2 border-background animate-pulse" />
              )}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-semibold text-foreground">{worker.name}</h3>
                <RoleIcon className={`w-3.5 h-3.5 ${info.color} opacity-80`} />
              </div>
              <p className="text-[10px] text-muted-foreground">{info.label}</p>
            </div>
          </div>
          <StatusBadge status={worker.status} pulse />
        </div>

        {/* Active task context */}
        {worker.current_task_title && (
          <div className="mb-3 bg-primary/5 border border-primary/10 rounded-lg p-2.5 space-y-0.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10px] font-semibold text-primary/80 uppercase tracking-wider flex items-center gap-1">
                <Zap className="w-2.5 h-2.5" /> Active Task
              </span>
              {elapsedTime && (
                <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                  <Clock className="w-2.5 h-2.5" /> {elapsedTime}
                </span>
              )}
            </div>
            <p className="text-[11px] text-foreground/90 leading-snug">{worker.current_task_title}</p>
          </div>
        )}

        {/* Stats grid */}
        <div className="grid grid-cols-3 gap-2 mb-3">
          <div className="glass-surface p-2 rounded-lg text-center min-w-0">
            <div className="text-xs font-medium text-foreground font-mono truncate" title={worker.model ?? worker.preferred_model ?? ''}>
              {worker.model ?? worker.preferred_model ?? '—'}
            </div>
            <div className="text-[9px] text-muted-foreground mt-0.5">Model</div>
          </div>
          <div className="glass-surface p-2 rounded-lg text-center min-w-0">
            <div className="text-xs font-medium text-foreground capitalize truncate" title={prov ?? ''}>
              {prov ?? '—'}
            </div>
            <div className="text-[9px] text-muted-foreground mt-0.5">Provider</div>
          </div>
          <div className="glass-surface p-2 rounded-lg text-center min-w-0">
            <div className={`text-xs font-medium ${totalTokens > 0 ? 'text-primary' : 'text-muted-foreground'}`}>
              {fmtTokens(totalTokens)}
            </div>
            <div className="text-[9px] text-muted-foreground mt-0.5">Tokens</div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-muted-foreground">
            ♡ {timeAgo(worker.heartbeat_at)}
          </span>
          {cfg && form && (
            <button
              type="button"
              onClick={() => setConfigOpen((v) => !v)}
              className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
              aria-expanded={configOpen}
            >
              Configure
              <ChevronDown className={`w-3 h-3 transition-transform duration-200 ${configOpen ? 'rotate-180' : ''}`} />
            </button>
          )}
        </div>

        {/* Inline config panel */}
        <AnimatePresence>
          {configOpen && cfg && form && (
            <RoleConfigPanel
              roleId={roleId}
              form={form}
              cfg={cfg}
              onChange={onChange}
              onSave={() => onSave(roleId)}
              onReset={() => onReset(roleId)}
              saveState={saveState}
              saveMsg={saveMsg}
            />
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

// ─── Offline Role Card ────────────────────────────────────────────────────────

interface OfflineRoleCardProps {
  roleId: string;
  cfg: AgentModelsResponse;
  form: RoleForm | undefined;
  onChange: (patch: Partial<RoleForm>) => void;
  onSave: (roleId: string) => Promise<void>;
  onReset: (roleId: string) => void;
  saveState: SaveState;
  saveMsg: string;
}

function OfflineRoleCard({ roleId, cfg, form, onChange, onSave, onReset, saveState, saveMsg }: OfflineRoleCardProps) {
  const [open, setOpen] = useState(false);
  const info = getRoleInfo(roleId);
  const RoleIcon = info.icon;

  return (
    <div className="glass-card border border-border/30 p-4 opacity-70 hover:opacity-100 transition-opacity">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="p-1.5 bg-muted/30 rounded-lg">
            <RoleIcon className={`w-4 h-4 ${info.color}`} />
          </div>
          <div>
            <p className="text-xs font-medium text-foreground">{info.label}</p>
            <p className="text-[10px] text-muted-foreground">{info.description}</p>
          </div>
        </div>
        {form && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-1 transition-colors"
          >
            Configure
            <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        )}
      </div>
      <AnimatePresence>
        {open && form && (
          <RoleConfigPanel
            roleId={roleId}
            form={form}
            cfg={cfg}
            onChange={onChange}
            onSave={() => onSave(roleId)}
            onReset={() => onReset(roleId)}
            saveState={saveState}
            saveMsg={saveMsg}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function AgentsPage() {
  const queryClient = useQueryClient();
  const { data: snapshot, isLoading, error } = useSnapshot();

  const { data: cfg, isLoading: cfgLoading } = useQuery({
    queryKey: ['settings', 'agent-models'],
    queryFn: fetchAgentModels,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  // Sort workers: active/claimed first, then idle, then others
  const workers = useMemo(() => {
    const all = snapshot?.workers ?? [];
    return [...all].sort((a, b) => {
      const rank = (s: string) => (s === 'active' || s === 'claimed' ? 0 : s === 'idle' ? 1 : 2);
      return rank(a.status) - rank(b.status);
    });
  }, [snapshot?.workers]);

  const activeCount = workers.filter((w) => w.status === 'claimed' || w.status === 'active').length;
  const idleCount = workers.filter((w) => w.status === 'idle').length;

  // Per-role form state: keyed by roleId (not workerId — each role shares its config)
  const [roleForms, setRoleForms] = useState<Record<string, RoleForm>>({});
  const [roleSaveState, setRoleSaveState] = useState<Record<string, SaveState>>({});
  const [roleSaveMsg, setRoleSaveMsg] = useState<Record<string, string>>({});
  const originalForms = useRef<Record<string, RoleForm>>({});

  // Populate forms when cfg loads
  useEffect(() => {
    if (!cfg) return;
    const next: Record<string, RoleForm> = {};
    const defP = cfg.settingsProvider;
    const defM = cfg.defaultLlmModel;
    const defH = cfg.defaultExecutionHost;
    for (const id of cfg.roleIds) {
      const effective = cfg.effective[id];
      const row = normalizeRoleSpec(effective, {
        credentialId: SETTINGS_DEFAULT_CREDENTIAL_ID,
        provider: defP,
        model: defM,
        host: defH,
      });
      next[id] = row;
    }
    setRoleForms(next);
    originalForms.current = next;
  }, [cfg]);

  const updateRoleForm = useCallback((roleId: string, patch: Partial<RoleForm>) => {
    setRoleForms((prev) => ({ ...prev, [roleId]: { ...prev[roleId], ...patch } }));
  }, []);

  const resetRole = useCallback((roleId: string) => {
    const orig = originalForms.current[roleId];
    if (orig) setRoleForms((prev) => ({ ...prev, [roleId]: orig }));
    setRoleSaveState((prev) => ({ ...prev, [roleId]: 'idle' }));
    setRoleSaveMsg((prev) => ({ ...prev, [roleId]: '' }));
  }, []);

  const saveRole = useCallback(async (roleId: string) => {
    const form = roleForms[roleId];
    if (!form) return;
    setRoleSaveState((prev) => ({ ...prev, [roleId]: 'saving' }));
    try {
      let rolePayload: Record<string, unknown>;
      if (form.useGlobal) {
        rolePayload = { useGlobal: true, executionHost: form.executionHost.trim() };
      } else {
        rolePayload = {
          credentialId: form.credentialId,
          provider: form.provider,
          model: form.model.trim(),
          executionHost: form.executionHost.trim(),
        };
      }
      const res = await fetch('/api/settings/agent-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roles: { [roleId]: rolePayload } }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error((body as { error?: string }).error ?? 'Save failed');
      setRoleSaveState((prev) => ({ ...prev, [roleId]: 'success' }));
      setRoleSaveMsg((prev) => ({ ...prev, [roleId]: '' }));
      originalForms.current[roleId] = form;
      await queryClient.invalidateQueries({ queryKey: ['settings', 'agent-models'] });
      // Auto-clear success after 4s
      setTimeout(() => setRoleSaveState((prev) => (prev[roleId] === 'success' ? { ...prev, [roleId]: 'idle' } : prev)), 4000);
    } catch (e) {
      setRoleSaveState((prev) => ({ ...prev, [roleId]: 'error' }));
      setRoleSaveMsg((prev) => ({ ...prev, [roleId]: e instanceof Error ? e.message : 'Save failed' }));
    }
  }, [roleForms, queryClient]);

  // Unique roles represented by current workers (for config availability)
  const workerRoles = useMemo(() => new Set(workers.map((w) => w.role)), [workers]);

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground">Agent Operations</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {isLoading
                ? 'Connecting…'
                : `${activeCount} active · ${idleCount} idle · ${workers.length} total`}
            </p>
          </div>
          {cfgLoading && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              Loading models…
            </div>
          )}
        </div>
      </motion.div>

      {/* States */}
      {isLoading && (
        <div className="flex items-center justify-center py-20 gap-3 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="text-sm">Connecting to workers…</span>
        </div>
      )}

      {error && (
        <div className="flex items-center gap-3 p-4 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          Failed to connect to backend.
        </div>
      )}

      {!isLoading && !error && workers.length === 0 && (
        <div className="glass-card-sweep border border-white/10 p-12 text-center flex flex-col items-center justify-center space-y-4">
          <div className="relative">
            <Bot className="w-10 h-10 text-primary/40 animate-pulse" />
            <div className="absolute inset-0 bg-primary/20 w-10 h-10 blur-xl rounded-full animate-pulse" />
          </div>
          <div>
            <h3 className="text-foreground font-semibold tracking-tight">Neural Agents Booting</h3>
            <p className="text-sm text-muted-foreground mt-1 max-w-md mx-auto">
              Workers are initializing. They will appear here once registered.
            </p>
          </div>
        </div>
      )}

      {/* Worker cards */}
      {!isLoading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 relative z-10">
          {workers.map((worker, i) => {
            const roleId = worker.role;
            return (
              <WorkerCard
                key={worker.name}
                worker={worker}
                index={i}
                cfg={cfg}
                form={roleForms[roleId]}
                onChange={(patch) => updateRoleForm(roleId, patch)}
                onSave={saveRole}
                onReset={resetRole}
                saveState={roleSaveState[roleId] ?? 'idle'}
                saveMsg={roleSaveMsg[roleId] ?? ''}
              />
            );
          })}
        </div>
      )}

      {/* Unconfigured roles (roles that have saved config but no live worker) */}
      {!isLoading && cfg && cfg.roleIds.filter((r) => !workerRoles.has(r)).length > 0 && (
        <div className="relative z-10">
          <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-3">
            Offline role defaults
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {cfg.roleIds
              .filter((r) => !workerRoles.has(r))
              .map((roleId) => (
                <OfflineRoleCard
                  key={roleId}
                  roleId={roleId}
                  cfg={cfg}
                  form={roleForms[roleId]}
                  onChange={(patch) => updateRoleForm(roleId, patch)}
                  onSave={saveRole}
                  onReset={resetRole}
                  saveState={roleSaveState[roleId] ?? 'idle'}
                  saveMsg={roleSaveMsg[roleId] ?? ''}
                />
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
