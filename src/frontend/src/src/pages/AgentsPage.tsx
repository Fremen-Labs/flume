import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

const SETTINGS_DEFAULT_CREDENTIAL_ID = '__settings_default__';
const OLLAMA_CREDENTIAL_ID = '__ollama__';
const OPENAI_OAUTH_CREDENTIAL_ID = '__openai_oauth__';
import { motion } from 'framer-motion';
import { Bot, Loader2, AlertCircle, Settings2, Info, ChevronRight } from 'lucide-react';

/** Change this when verifying the Agents page bundle deployed to your environment. */
const AGENTS_UI_BUILD_STAMP = '2026-03-19';
import { useSnapshot } from '@/hooks/useSnapshot';
import { StatusBadge } from '@/components/StatusBadge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
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
  AgentModelsSavePayload,
} from '@/types';

import agentAvatar1 from '@/assets/agents/agent-1.png';
import agentAvatar2 from '@/assets/agents/agent-2.png';
import agentAvatar3 from '@/assets/agents/agent-3.png';
import agentAvatar4 from '@/assets/agents/agent-4.png';

const agentAvatars = [agentAvatar1, agentAvatar2, agentAvatar3, agentAvatar4];

const roleLabels: Record<string, string> = {
  intake: 'Intake Agent',
  pm: 'Project Manager',
  implementer: 'Implementer',
  tester: 'Tester',
  reviewer: 'Code Reviewer',
  'memory-updater': 'Memory Updater',
  planner: 'Planner',
  architect: 'Architect',
  devops: 'DevOps',
  qa: 'QA Engineer',
};

type RoleForm = {
  credentialId: string;
  provider: string;
  model: string;
  executionHost: string;
};

function normalizeRoleSpec(
  raw: unknown,
  fallback: { credentialId: string; provider: string; model: string; host: string },
): RoleForm {
  if (typeof raw === 'string') {
    return {
      credentialId: fallback.credentialId,
      provider: fallback.provider,
      model: raw || fallback.model,
      executionHost: fallback.host,
    };
  }
  if (raw && typeof raw === 'object') {
    const o = raw as AgentModelsRoleEffective;
    return {
      credentialId: o.credentialId || fallback.credentialId,
      provider: o.provider || fallback.provider,
      model: o.model || fallback.model,
      executionHost: o.executionHost || fallback.host,
    };
  }
  return {
    credentialId: fallback.credentialId,
    provider: fallback.provider,
    model: fallback.model,
    executionHost: fallback.host,
  };
}

function timeAgo(ts?: string) {
  if (!ts) return 'unknown';
  const diff = Date.now() - new Date(ts).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

async function fetchAgentModelsOverview(): Promise<AgentModelsResponse> {
  const res = await fetch('/api/settings/agent-models');
  if (!res.ok) throw new Error(`agent-models: ${res.status}`);
  return res.json();
}

function credentialShortName(cfg: AgentModelsResponse, credId?: string): string {
  const cid = credId || '';
  if (!cid || cid === SETTINGS_DEFAULT_CREDENTIAL_ID) return 'Settings default';
  if (cid === OLLAMA_CREDENTIAL_ID) return 'Ollama';
  if (cid === OPENAI_OAUTH_CREDENTIAL_ID) return 'OpenAI OAuth';
  const g = cfg.availableCredentials?.find((c) => c.credentialId === cid);
  return g?.shortLabel || g?.label || cid;
}

export default function AgentsPage() {
  const queryClient = useQueryClient();
  const { data: snapshot, isLoading, error } = useSnapshot();
  const workers = snapshot?.workers ?? [];

  const {
    data: modelsOverview,
    isLoading: modelsOverviewLoading,
    error: modelsOverviewError,
  } = useQuery({
    queryKey: ['settings', 'agent-models'],
    queryFn: fetchAgentModelsOverview,
    staleTime: 20_000,
  });

  const perRoleSummary = useMemo(() => {
    if (!modelsOverview?.roleIds?.length) return null;
    const rows = modelsOverview.roleIds
      .map((id) => {
        const e = modelsOverview.effective[id];
        if (!e) return null;
        return { roleId: id, ...e };
      })
      .filter(Boolean) as (AgentModelsRoleEffective & { roleId: string })[];
    if (rows.length < 2) return { rows, allSame: false };
    const f = rows[0];
    const allSame = rows.every(
      (r) =>
        r.provider === f.provider &&
        r.model === f.model &&
        (r.credentialId || SETTINGS_DEFAULT_CREDENTIAL_ID) === (f.credentialId || SETTINGS_DEFAULT_CREDENTIAL_ID),
    );
    return { rows, allSame };
  }, [modelsOverview]);

  const [configOpen, setConfigOpen] = useState(false);
  const [agentCfg, setAgentCfg] = useState<AgentModelsResponse | null>(null);
  const [roleForm, setRoleForm] = useState<Record<string, RoleForm>>({});
  const [cfgLoading, setCfgLoading] = useState(false);
  const [cfgError, setCfgError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  const loadAgentModels = useCallback(async () => {
    setCfgLoading(true);
    setCfgError(null);
    try {
      const res = await fetch('/api/settings/agent-models');
      if (!res.ok) {
        const t = await res.text();
        throw new Error(t.slice(0, 200) || res.statusText);
      }
      const data: AgentModelsResponse = await res.json();
      setAgentCfg(data);
      const next: Record<string, RoleForm> = {};
      const defP = data.settingsProvider;
      const defM = data.defaultLlmModel;
      const defH = data.defaultExecutionHost;
      const allCred: AgentModelsCredentialGroup[] = data.availableCredentials ?? [];
      const hasCatalog = allCred.length > 0;
      const credChoices: AgentModelsCredentialGroup[] = allCred.filter((g) => g.configured);
      const useCredentialPicker = hasCatalog && credChoices.length > 0;
      for (const id of data.roleIds) {
        let row = normalizeRoleSpec(data.effective[id], {
          credentialId: SETTINGS_DEFAULT_CREDENTIAL_ID,
          provider: defP,
          model: defM,
          host: defH,
        });
        if (useCredentialPicker) {
          if (!credChoices.some((g) => g.credentialId === row.credentialId)) {
            const first = credChoices[0];
            if (first) {
              row = {
                ...row,
                credentialId: first.credentialId,
                provider: first.providerId,
                model: first.models[0]?.id || row.model,
              };
            }
          }
          const cg = credChoices.find((x) => x.credentialId === row.credentialId);
          if (cg) {
            row = { ...row, provider: cg.providerId };
            if (!cg.allowCustomModelId && cg.models?.length) {
              const ok = cg.models.some((m) => m.id === row.model);
              if (!ok) row = { ...row, model: cg.models[0].id };
            }
          }
        } else {
          const configured = data.availableProviders.filter((g) => g.configured);
          if (!configured.some((g) => g.providerId === row.provider)) {
            const first = configured[0];
            if (first) {
              row = {
                ...row,
                provider: first.providerId,
                model: first.models[0]?.id || row.model,
              };
            }
          }
          const g = configured.find((x) => x.providerId === row.provider);
          if (g && !g.allowCustomModelId && g.models?.length) {
            const ok = g.models.some((m) => m.id === row.model);
            if (!ok) row = { ...row, model: g.models[0].id };
          }
        }
        next[id] = row;
      }
      setRoleForm(next);
    } catch (e) {
      setCfgError(e instanceof Error ? e.message : 'Failed to load');
      setAgentCfg(null);
    } finally {
      setCfgLoading(false);
    }
  }, []);

  useEffect(() => {
    if (configOpen) void loadAgentModels();
  }, [configOpen, loadAgentModels]);

  const selectableProviders = useMemo(
    () => agentCfg?.availableProviders.filter((g) => g.configured) ?? [],
    [agentCfg],
  );

  const allCredentials = useMemo(() => agentCfg?.availableCredentials ?? [], [agentCfg]);

  const selectableCredentials = useMemo(
    () => allCredentials.filter((g) => g.configured),
    [allCredentials],
  );

  const incompleteCredentials = useMemo(
    () => allCredentials.filter((g) => !g.configured),
    [allCredentials],
  );

  /** API returned credential catalog (includes incomplete rows for visibility). */
  const hasCredentialCatalog = allCredentials.length > 0;
  /** Enough ready connections to use Provider → Key → Model pickers. */
  const canPickCredentials = selectableCredentials.length > 0;

  const vendorOptions = useMemo(() => {
    // Every saved key + settings default row — not only "configured" rows — so all vendors appear.
    const ids = [...new Set(allCredentials.map((g) => g.providerId))].sort();
    return ids.map((id) => ({
      id,
      label:
        selectableProviders.find((p) => p.providerId === id)?.label ??
        id
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (ch) => ch.toUpperCase()),
    }));
  }, [allCredentials, selectableProviders]);

  const updateRole = (roleId: string, patch: Partial<RoleForm>) => {
    setRoleForm((prev) => {
      const cur = prev[roleId];
      if (!cur) return prev;
      return { ...prev, [roleId]: { ...cur, ...patch } };
    });
  };

  const onProviderChange = (roleId: string, providerId: string) => {
    const g = selectableProviders.find((x) => x.providerId === providerId);
    const firstModel = g?.models?.[0]?.id ?? '';
    updateRole(roleId, { provider: providerId, model: firstModel });
  };

  const onVendorChange = (roleId: string, vendorId: string) => {
    const keys = allCredentials.filter((g) => g.providerId === vendorId);
    const firstReady = keys.find((g) => g.configured);
    if (firstReady) {
      updateRole(roleId, {
        provider: vendorId,
        credentialId: firstReady.credentialId,
        model: firstReady.models[0]?.id ?? '',
      });
      return;
    }
    const settingsDef = allCredentials.find((g) => g.credentialId === SETTINGS_DEFAULT_CREDENTIAL_ID);
    if (settingsDef?.configured && settingsDef.providerId === vendorId) {
      updateRole(roleId, {
        provider: vendorId,
        credentialId: SETTINGS_DEFAULT_CREDENTIAL_ID,
        model: settingsDef.models[0]?.id ?? '',
      });
      return;
    }
    const fallback = keys[0];
    if (!fallback) return;
    updateRole(roleId, {
      provider: vendorId,
      credentialId: fallback.credentialId,
      model: fallback.models[0]?.id ?? '',
    });
  };

  const onCredentialPick = (roleId: string, credentialId: string) => {
    const g = allCredentials.find((x) => x.credentialId === credentialId);
    if (!g || !g.configured) return;
    updateRole(roleId, {
      provider: g.providerId,
      credentialId,
      model: g.models[0]?.id ?? '',
    });
  };

  const saveAgentModels = async () => {
    if (!agentCfg) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const credCatalog = agentCfg.availableCredentials ?? [];
      const credReady = credCatalog.filter((g) => g.configured);
      const hasCat = credCatalog.length > 0;
      const canPick = credReady.length > 0;
      const roles: AgentModelsSavePayload['roles'] = {};
      for (const id of agentCfg.roleIds) {
        const s = roleForm[id];
        if (!s) continue;
        if (hasCat && canPick) {
          roles[id] = {
            credentialId: s.credentialId,
            model: s.model.trim(),
            executionHost: s.executionHost.trim(),
          };
        } else {
          roles[id] = {
            provider: s.provider,
            model: s.model.trim(),
            executionHost: s.executionHost.trim(),
          };
        }
      }
      const res = await fetch('/api/settings/agent-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roles }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error((body as { error?: string }).error || 'Save failed');
      }
      setSaveMsg('Saved. Worker manager picks this up on its next cycle (no restart).');
      await loadAgentModels();
      await queryClient.invalidateQueries({ queryKey: ['settings', 'agent-models'] });
    } catch (e) {
      setSaveMsg(e instanceof Error ? e.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const activeCount = workers.filter((w) => w.status === 'claimed' || w.status === 'active').length;
  const idleCount = workers.filter((w) => w.status === 'idle').length;

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10 space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground">Agent Operations</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {isLoading
                ? 'Loading…'
                : `${activeCount} active · ${idleCount} idle · ${workers.length} total`}
            </p>
            <p
              className="text-[10px] font-mono text-primary/70 dark:text-primary/60 mt-1.5 tracking-wide"
              title="If this date matches your deploy, the new Agents UI bundle is live."
            >
              UI build {AGENTS_UI_BUILD_STAMP}
            </p>
          </div>
        </div>

        <div className="rounded-xl border-[3px] border-primary/70 bg-gradient-to-br from-primary/20 via-primary/12 to-primary/5 dark:from-primary/30 dark:via-primary/18 dark:to-primary/8 p-5 sm:p-6 shadow-xl shadow-primary/10 ring-4 ring-primary/15">
          <div className="flex flex-col gap-5">
            <div className="flex-1 min-w-0 space-y-2">
              <p className="text-lg sm:text-xl font-bold text-foreground tracking-tight">
                Configure each agent&apos;s model, provider &amp; API key
              </p>
              <p className="text-sm text-muted-foreground leading-relaxed">
                Each worker (e.g. <code className="text-xs bg-background/60 px-1 py-0.5 rounded">intake-worker-1</code>)
                maps to one <strong>role</strong>. Open the editor to set them <strong>independently</strong> — not from
                the cards below. Cards update after the worker manager&apos;s next poll.
              </p>
            </div>
            <Button
              type="button"
              size="lg"
              className="w-full gap-3 min-h-[3.75rem] sm:min-h-[4rem] px-8 text-base sm:text-lg font-bold shadow-lg shadow-primary/25 hover:shadow-xl hover:shadow-primary/30 hover:brightness-105 active:brightness-95 border-2 border-primary-foreground/15"
              onClick={() => setConfigOpen(true)}
            >
              <Settings2 className="w-6 h-6 sm:w-7 sm:h-7 shrink-0" aria-hidden />
              <span className="flex-1 text-center sm:text-left">Configure agent models</span>
              <ChevronRight className="w-6 h-6 sm:w-7 sm:h-7 shrink-0 opacity-90" aria-hidden />
            </Button>
          </div>
        </div>

        <div>
            {modelsOverviewLoading && (
              <p className="text-xs text-muted-foreground mt-2 flex items-center gap-2">
                <Loader2 className="h-3 w-3 animate-spin shrink-0" />
                Loading saved per-role LLM…
              </p>
            )}
            {modelsOverviewError && (
              <p className="text-xs text-destructive/90 mt-2">
                Could not load per-role configuration ({modelsOverviewError instanceof Error ? modelsOverviewError.message : 'error'}).
              </p>
            )}
            {perRoleSummary && perRoleSummary.rows.length > 0 && (
              <div className="mt-4 space-y-3 max-w-4xl">
                {perRoleSummary.allSame && (
                  <div className="flex gap-2 rounded-md border border-amber-500/35 bg-amber-500/10 px-3 py-2 text-xs text-amber-950 dark:text-amber-100">
                    <Info className="h-4 w-4 shrink-0 opacity-80 mt-0.5" />
                    <div className="flex flex-col sm:flex-row sm:items-center gap-3 min-w-0">
                      <p className="min-w-0 flex-1">
                        <strong>Every role is identical</strong> (same provider, model, and key profile). Set each row
                        differently — e.g. Intake = <code className="text-[10px]">gpt-4o-mini</code>, PM ={' '}
                        <code className="text-[10px]">gpt-4o</code>, or different <strong>vendors</strong> via saved
                        keys.
                      </p>
                      <Button type="button" size="sm" className="shrink-0" onClick={() => setConfigOpen(true)}>
                        Open editor
                      </Button>
                    </div>
                  </div>
                )}
                <div className="rounded-lg border border-border/40 bg-muted/5 overflow-hidden">
                  <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 border-b border-border/30 bg-muted/10">
                    <span className="text-xs font-medium text-foreground">Saved LLM per role</span>
                    <Button type="button" size="sm" className="h-8 text-xs font-medium" onClick={() => setConfigOpen(true)}>
                      Configure…
                    </Button>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-left text-muted-foreground border-b border-border/30">
                          <th className="px-3 py-2 font-medium">Role</th>
                          <th className="px-3 py-2 font-medium">Provider</th>
                          <th className="px-3 py-2 font-medium">Model</th>
                          <th className="px-3 py-2 font-medium">Key / profile</th>
                          <th className="px-3 py-2 font-medium">Host</th>
                        </tr>
                      </thead>
                      <tbody>
                        {perRoleSummary.rows.map((r) => (
                          <tr key={r.roleId} className="border-b border-border/20 last:border-0">
                            <td className="px-3 py-2 font-medium text-foreground">
                              {roleLabels[r.roleId] ?? r.roleId}
                            </td>
                            <td className="px-3 py-2 capitalize">{r.provider}</td>
                            <td className="px-3 py-2 font-mono text-[11px]">{r.model}</td>
                            <td className="px-3 py-2 text-muted-foreground">
                              {modelsOverview ? credentialShortName(modelsOverview, r.credentialId) : '—'}
                            </td>
                            <td className="px-3 py-2 text-muted-foreground">{r.executionHost || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            )}
          </div>

        <Dialog open={configOpen} onOpenChange={setConfigOpen}>
            <DialogContent className="max-w-4xl w-[95vw] max-h-[88vh] flex flex-col gap-0 p-0 overflow-hidden">
              <DialogHeader className="px-6 pt-6 pb-2 shrink-0">
                <DialogTitle>Agent models & hosts</DialogTitle>
                <DialogDescription>
                  <strong>Settings → LLM</strong> defines the default profile. Here you <strong>override per role</strong>{' '}
                  (each row matches <code className="text-[10px]">intake-worker-1</code>,{' '}
                  <code className="text-[10px]">pm-worker-1</code>, …). Pick <strong>Provider</strong>, then a{' '}
                  <strong>saved key</strong> (or Settings default / OpenAI OAuth), then a <strong>model</strong> from the
                  catalog. If a saved key is removed, that role falls back to <strong>Settings default</strong>{' '}
                  automatically.
                </DialogDescription>
              </DialogHeader>
              <div className="px-6 flex-1 min-h-0 overflow-y-auto py-2 space-y-4">
                {cfgLoading && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground py-8 justify-center">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    Loading options…
                  </div>
                )}
                {cfgError && (
                  <div className="flex items-center gap-2 text-sm text-destructive py-4">
                    <AlertCircle className="w-4 h-4 shrink-0" />
                    {cfgError}
                  </div>
                )}
                {!cfgLoading && !cfgError && !hasCredentialCatalog && selectableProviders.length === 0 && (
                  <p className="text-sm text-muted-foreground py-4">
                    No LLM providers are configured. Open Settings → LLM and add API keys or OAuth, then return here.
                  </p>
                )}
                {!cfgLoading && !cfgError && hasCredentialCatalog && !canPickCredentials && (
                  <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-3 text-sm space-y-2">
                    <p className="font-medium text-amber-950 dark:text-amber-100">
                      Saved key profiles need an API key before agents can use them
                    </p>
                    <p className="text-xs text-muted-foreground">
                      Open <strong>Settings → LLM</strong>, select each provider, and paste keys for the labels below. You
                      can still use <strong>Settings (default)</strong> here if your global Settings profile is already
                      configured — switch to the legacy form below if it appears.
                    </p>
                    {incompleteCredentials.length > 0 && (
                      <ul className="text-xs list-disc pl-4 space-y-1 text-foreground/90">
                        {incompleteCredentials.map((g) => (
                          <li key={g.credentialId}>
                            <strong>{g.shortLabel ?? g.label}</strong> ({g.providerId})
                            {g.hint ? ` — ${g.hint}` : ''}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
                {!cfgLoading &&
                  !cfgError &&
                  agentCfg &&
                  hasCredentialCatalog &&
                  canPickCredentials &&
                  agentCfg.roleIds.map((roleId) => {
                    const spec = roleForm[roleId];
                    if (!spec) return null;
                    const keysThisVendor = allCredentials.filter((g) => g.providerId === spec.provider);
                    const readyKeys = keysThisVendor.filter((k) => k.configured);
                    const credGroup =
                      keysThisVendor.find((g) => g.credentialId === spec.credentialId) ??
                      readyKeys[0] ??
                      keysThisVendor[0];
                    const group = credGroup;
                    const keySelectValue = readyKeys.some((k) => k.credentialId === spec.credentialId)
                      ? spec.credentialId
                      : (readyKeys[0]?.credentialId ?? '');
                    const allowCustom = group?.allowCustomModelId === true;
                    const useModelInput = allowCustom || !group?.models?.length;
                    return (
                      <div
                        key={roleId}
                        className="rounded-lg border border-border/40 bg-muted/5 p-4 space-y-3"
                      >
                        <div className="text-sm font-medium text-foreground">
                          {roleLabels[roleId] ?? roleId}
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
                          <div className="space-y-1.5">
                            <Label className="text-xs">Provider</Label>
                            <Select value={spec.provider} onValueChange={(v) => onVendorChange(roleId, v)}>
                              <SelectTrigger className="h-9">
                                <SelectValue placeholder="Provider" />
                              </SelectTrigger>
                              <SelectContent>
                                {vendorOptions.map((v) => (
                                  <SelectItem key={v.id} value={v.id}>
                                    {v.label}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </div>
                          <div className="space-y-1.5">
                            <Label className="text-xs">Saved API key</Label>
                            {keysThisVendor.length === 0 ? (
                              <p className="text-[11px] text-muted-foreground py-1">
                                No keys for this vendor — add them in Settings → LLM for that provider.
                              </p>
                            ) : readyKeys.length === 0 ? (
                              <p className="text-[11px] text-destructive/90 py-1">
                                No API key pasted yet for this provider. Open Settings → LLM, select this provider, and
                                save a key — grayed labels below are previews only.
                              </p>
                            ) : (
                              <Select
                                value={keySelectValue || readyKeys[0]!.credentialId}
                                onValueChange={(v) => onCredentialPick(roleId, v)}
                              >
                                <SelectTrigger className="h-9">
                                  <SelectValue placeholder="Saved key" />
                                </SelectTrigger>
                                <SelectContent>
                                  {keysThisVendor.map((g) => (
                                    <SelectItem
                                      key={g.credentialId}
                                      value={g.credentialId}
                                      disabled={!g.configured}
                                      className={!g.configured ? 'opacity-50' : undefined}
                                    >
                                      {g.shortLabel ?? g.label}
                                      {g.keySuffix ? ` · ···${g.keySuffix}` : ''}
                                      {g.credentialId === SETTINGS_DEFAULT_CREDENTIAL_ID ? ' (Settings default)' : ''}
                                      {!g.configured ? ' — add key in Settings' : ''}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                            {group?.hint && (
                              <p className="text-[10px] text-muted-foreground leading-tight">{group.hint}</p>
                            )}
                          </div>
                          <div className="space-y-1.5">
                            <Label className="text-xs">Model</Label>
                            {useModelInput ? (
                              <Input
                                className="h-9"
                                value={spec.model}
                                onChange={(e) => updateRole(roleId, { model: e.target.value })}
                                placeholder="Model id"
                              />
                            ) : (
                              <Select value={spec.model} onValueChange={(v) => updateRole(roleId, { model: v })}>
                                <SelectTrigger className="h-9">
                                  <SelectValue placeholder="Model" />
                                </SelectTrigger>
                                <SelectContent>
                                  {(group?.models ?? []).map((m) => (
                                    <SelectItem key={m.id} value={m.id}>
                                      {m.name || m.id}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                          </div>
                          <div className="space-y-1.5">
                            <Label className="text-xs">Execution host</Label>
                            <Input
                              className="h-9"
                              value={spec.executionHost}
                              onChange={(e) => updateRole(roleId, { executionHost: e.target.value })}
                              placeholder={agentCfg.defaultExecutionHost}
                            />
                          </div>
                        </div>
                      </div>
                    );
                  })}
                {!cfgLoading &&
                  !cfgError &&
                  agentCfg &&
                  (!hasCredentialCatalog || !canPickCredentials) &&
                  selectableProviders.length > 0 &&
                  agentCfg.roleIds.map((roleId) => {
                    const spec = roleForm[roleId];
                    if (!spec) return null;
                    const group = selectableProviders.find((g) => g.providerId === spec.provider);
                    const allowCustom = group?.allowCustomModelId === true;
                    const useModelInput = allowCustom || !group?.models?.length;
                    return (
                      <div
                        key={roleId}
                        className="rounded-lg border border-border/40 bg-muted/5 p-4 space-y-3"
                      >
                        <div className="text-sm font-medium text-foreground">
                          {roleLabels[roleId] ?? roleId}
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                          <div className="space-y-1.5">
                            <Label className="text-xs">Provider</Label>
                            <Select value={spec.provider} onValueChange={(v) => onProviderChange(roleId, v)}>
                              <SelectTrigger className="h-9">
                                <SelectValue placeholder="Provider" />
                              </SelectTrigger>
                              <SelectContent>
                                {selectableProviders.map((g) => (
                                  <SelectItem key={g.providerId} value={g.providerId}>
                                    {g.label}
                                    {g.isPrimary ? ' (primary)' : ''}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {group?.hint && (
                              <p className="text-[10px] text-muted-foreground leading-tight">{group.hint}</p>
                            )}
                          </div>
                          <div className="space-y-1.5">
                            <Label className="text-xs">Model</Label>
                            {useModelInput ? (
                              <Input
                                className="h-9"
                                value={spec.model}
                                onChange={(e) => updateRole(roleId, { model: e.target.value })}
                                placeholder="Model id"
                              />
                            ) : (
                              <Select value={spec.model} onValueChange={(v) => updateRole(roleId, { model: v })}>
                                <SelectTrigger className="h-9">
                                  <SelectValue placeholder="Model" />
                                </SelectTrigger>
                                <SelectContent>
                                  {(group?.models ?? []).map((m) => (
                                    <SelectItem key={m.id} value={m.id}>
                                      {m.name || m.id}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            )}
                          </div>
                          <div className="space-y-1.5">
                            <Label className="text-xs">Execution host</Label>
                            <Input
                              className="h-9"
                              value={spec.executionHost}
                              onChange={(e) => updateRole(roleId, { executionHost: e.target.value })}
                              placeholder={agentCfg.defaultExecutionHost}
                            />
                          </div>
                        </div>
                      </div>
                    );
                  })}
              </div>
              <DialogFooter className="px-6 py-4 border-t border-border/40 shrink-0 flex-col sm:flex-row gap-2 items-stretch sm:items-center">
                {saveMsg && (
                  <p
                    className={`text-xs mr-auto ${saveMsg.startsWith('Saved') ? 'text-emerald-600' : 'text-destructive'}`}
                  >
                    {saveMsg}
                  </p>
                )}
                <Button variant="outline" type="button" onClick={() => setConfigOpen(false)}>
                  Close
                </Button>
                <Button
                  type="button"
                  disabled={
                    saving ||
                    cfgLoading ||
                    !!cfgError ||
                    !agentCfg ||
                    (hasCredentialCatalog && canPickCredentials
                      ? false
                      : selectableProviders.length === 0)
                  }
                  onClick={() => void saveAgentModels()}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Save'}
                </Button>
              </DialogFooter>
            </DialogContent>
        </Dialog>
      </motion.div>

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
        <div className="glass-card p-12 text-center text-sm text-muted-foreground">
          <Bot className="w-8 h-8 mx-auto mb-3 opacity-30" />
          No workers running. Start the worker manager to see agents here.
        </div>
      )}

      {!isLoading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 relative z-10">
          {workers.map((worker, i) => {
            const isActive = worker.status === 'claimed' || worker.status === 'active';
            const prov = worker.llm_provider ?? worker.preferred_llm_provider;
            return (
              <motion.div
                key={worker.name}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.03 }}
                whileHover={{ y: -3, transition: { duration: 0.2 } }}
                className={`glass-card-glow p-5 hover-lift relative ${isActive ? 'agent-breathing' : ''}`}
              >
                <div className="flex items-start justify-between mb-3 relative z-10">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-full overflow-hidden ring-1 ring-primary/20">
                      <img src={agentAvatars[i % agentAvatars.length]} alt={worker.name} className="w-full h-full object-cover" />
                    </div>
                    <div>
                      <h3 className="text-sm font-semibold text-foreground">{worker.name}</h3>
                      <p className="text-[10px] text-muted-foreground">{roleLabels[worker.role] ?? worker.role}</p>
                    </div>
                  </div>
                  <StatusBadge status={worker.status} pulse />
                </div>

                {worker.current_task_title && (
                  <div className="text-[11px] text-muted-foreground mb-3 relative z-10 bg-muted/10 rounded p-2 border border-border/30">
                    <span className="text-[10px] font-medium text-primary/80 block mb-0.5">Current Task</span>
                    {worker.current_task_title}
                  </div>
                )}

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 relative z-10">
                  <div className="glass-surface p-2 rounded text-center min-w-0">
                    <div className="text-xs font-medium text-foreground truncate" title={worker.model ?? worker.preferred_model}>
                      {worker.model ?? worker.preferred_model ?? '—'}
                    </div>
                    <div className="text-[10px] text-muted-foreground">Model</div>
                  </div>
                  <div className="glass-surface p-2 rounded text-center min-w-0">
                    <div className="text-xs font-medium text-foreground truncate capitalize" title={prov}>
                      {prov ?? '—'}
                    </div>
                    <div className="text-[10px] text-muted-foreground">Provider</div>
                  </div>
                  <div className="glass-surface p-2 rounded text-center min-w-0">
                    <div
                      className="text-xs font-medium text-foreground truncate"
                      title={worker.llm_credential_label ?? worker.preferred_llm_credential_id ?? ''}
                    >
                      {worker.llm_credential_label ?? worker.preferred_llm_credential_id ?? '—'}
                    </div>
                    <div className="text-[10px] text-muted-foreground">API key</div>
                  </div>
                  <div className="glass-surface p-2 rounded text-center min-w-0">
                    <div className="text-xs font-medium text-foreground truncate" title={worker.execution_host}>
                      {worker.execution_host ?? '—'}
                    </div>
                    <div className="text-[10px] text-muted-foreground">Host</div>
                  </div>
                </div>

                <div className="mt-3 flex items-center justify-between text-[10px] text-muted-foreground relative z-10">
                  <span>Heartbeat: {timeAgo(worker.heartbeat_at)}</span>
                  {worker.current_task_id && (
                    <code className="text-primary/70 truncate max-w-[45%]">{worker.current_task_id}</code>
                  )}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}
