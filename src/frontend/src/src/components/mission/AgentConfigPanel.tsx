import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { CheckCircle2, XCircle, RotateCcw, Loader2, Save } from 'lucide-react';
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

export const SETTINGS_DEFAULT_CREDENTIAL_ID = '__settings_default__';
export const OLLAMA_CREDENTIAL_ID = '__ollama__';
export const OPENAI_OAUTH_CREDENTIAL_ID = '__openai_oauth__';

export type RoleForm = {
  useGlobal: boolean;
  credentialId: string;
  provider: string;
  model: string;
  executionHost: string;
};

export type SaveState = 'idle' | 'saving' | 'success' | 'error';

export function normalizeRoleSpec(
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

export async function fetchAgentModels(): Promise<AgentModelsResponse> {
  const res = await fetch('/api/settings/agent-models');
  if (!res.ok) throw new Error(`agent-models: ${res.status}`);
  return res.json();
}

interface RoleModelPickerProps {
  form: RoleForm;
  cfg: AgentModelsResponse;
  onChange: (patch: Partial<RoleForm>) => void;
}

export function RoleModelPicker({ form, cfg, onChange }: RoleModelPickerProps) {
  const credentials: AgentModelsCredentialGroup[] = cfg.availableCredentials ?? [];
  const readyCredentials = credentials.filter((c) => c.configured);
  const hasCredentials = readyCredentials.length > 0;

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
            <SelectTrigger className="h-9 text-xs bg-background/50 border-white/10">
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
              className="h-9 text-xs font-mono bg-background/50 border-white/10"
              value={form.model}
              onChange={(e) => onChange({ model: e.target.value })}
              placeholder="e.g. qwen2.5-coder:7b"
            />
          ) : (
            <Select value={form.model} onValueChange={(v) => onChange({ model: v })}>
              <SelectTrigger className="h-9 text-xs bg-background/50 border-white/10">
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
          <SelectTrigger className="h-9 text-xs bg-background/50 border-white/10">
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
            className="h-9 text-xs font-mono bg-background/50 border-white/10"
            value={form.model}
            onChange={(e) => onChange({ model: e.target.value })}
            placeholder="e.g. qwen2.5-coder:7b"
          />
        ) : (
          <Select value={form.model} onValueChange={(v) => onChange({ model: v })}>
            <SelectTrigger className="h-9 text-xs bg-background/50 border-white/10">
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

export function RoleConfigPanel({ roleId, form, cfg, onChange, onSave, onReset, saveState, saveMsg }: RoleConfigPanelProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.22, ease: 'easeInOut' }}
      className="overflow-hidden"
    >
      <div className="mt-4 pt-4 border-t border-border/10 space-y-4">
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
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <RoleModelPicker form={form} cfg={cfg} onChange={onChange} />
            <div className="space-y-1.5 sm:col-span-2">
              <Label className="text-xs text-muted-foreground">Execution host</Label>
              <Input
                className="h-9 text-xs font-mono bg-background/50 border-white/10"
                value={form.executionHost}
                onChange={(e) => onChange({ executionHost: e.target.value })}
                placeholder={cfg.defaultExecutionHost || 'e.g. 127.0.0.1'}
              />
            </div>
          </div>
        )}

        <div className="flex items-center justify-between gap-3 pt-2">
          <div className="flex items-center gap-2 min-w-0">
            {saveState === 'success' && (
              <span className="flex items-center gap-1 text-xs text-emerald-500">
                <CheckCircle2 className="w-3.5 h-3.5" /> Saved.
              </span>
            )}
            {saveState === 'error' && (
              <span className="flex items-center gap-1 text-xs text-destructive truncate max-w-[150px]">
                <XCircle className="w-3.5 h-3.5" /> {saveMsg}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 gap-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-white/5"
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
              Save
            </Button>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
