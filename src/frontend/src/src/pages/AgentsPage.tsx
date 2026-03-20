import { useCallback, useEffect, useMemo, useState } from 'react';

const SETTINGS_DEFAULT_CREDENTIAL_ID = '__settings_default__';
import { motion } from 'framer-motion';
import { Bot, Loader2, AlertCircle, Settings2 } from 'lucide-react';
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
  DialogTrigger,
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

export default function AgentsPage() {
  const { data: snapshot, isLoading, error } = useSnapshot();
  const workers = snapshot?.workers ?? [];

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
      const credChoices: AgentModelsCredentialGroup[] =
        data.availableCredentials?.filter((g) => g.configured) ?? [];
      const useCredUi = credChoices.length > 0;
      for (const id of data.roleIds) {
        let row = normalizeRoleSpec(data.effective[id], {
          credentialId: SETTINGS_DEFAULT_CREDENTIAL_ID,
          provider: defP,
          model: defM,
          host: defH,
        });
        if (useCredUi) {
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

  const selectableCredentials = useMemo(
    () => agentCfg?.availableCredentials?.filter((g) => g.configured) ?? [],
    [agentCfg],
  );

  const useCredUi = selectableCredentials.length > 0;

  const vendorOptions = useMemo(() => {
    const ids = [...new Set(selectableCredentials.map((g) => g.providerId))];
    return ids.map((id) => ({
      id,
      label:
        selectableProviders.find((p) => p.providerId === id)?.label ??
        id
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (ch) => ch.toUpperCase()),
    }));
  }, [selectableCredentials, selectableProviders]);

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
    const keys = selectableCredentials.filter((g) => g.providerId === vendorId);
    const first = keys[0];
    if (!first) return;
    updateRole(roleId, {
      provider: vendorId,
      credentialId: first.credentialId,
      model: first.models[0]?.id ?? '',
    });
  };

  const onCredentialPick = (roleId: string, credentialId: string) => {
    const g = selectableCredentials.find((x) => x.credentialId === credentialId);
    if (!g) return;
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
      const roles: AgentModelsSavePayload['roles'] = {};
      for (const id of agentCfg.roleIds) {
        const s = roleForm[id];
        if (!s) continue;
        if (useCredUi) {
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
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground">Agent Operations</h1>
            <p className="text-sm text-muted-foreground mt-1">
              {isLoading
                ? 'Loading…'
                : `${activeCount} active · ${idleCount} idle · ${workers.length} total`}
            </p>
          </div>
          <Dialog open={configOpen} onOpenChange={setConfigOpen}>
            <DialogTrigger asChild>
              <Button variant="outline" size="sm" className="shrink-0 gap-2">
                <Settings2 className="w-4 h-4" />
                Configure agent models
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-4xl w-[95vw] max-h-[88vh] flex flex-col gap-0 p-0 overflow-hidden">
              <DialogHeader className="px-6 pt-6 pb-2 shrink-0">
                <DialogTitle>Agent models & hosts</DialogTitle>
                <DialogDescription>
                  Each <strong>row</strong> is one agent <strong>role</strong> (intake, implementer, …). Pick a{' '}
                  <strong>vendor</strong>, then a <strong>saved API key</strong> (labeled in Settings → LLM), then a{' '}
                  <strong>model</strong>. Roles can use different keys; the worker manager applies this on every cycle
                  (no restart). The cards below show which key each worker is using.
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
                {!cfgLoading && !cfgError && !useCredUi && selectableProviders.length === 0 && (
                  <p className="text-sm text-muted-foreground py-4">
                    No LLM providers are configured. Open Settings → LLM and add API keys or OAuth, then return here.
                  </p>
                )}
                {!cfgLoading && !cfgError && useCredUi && selectableCredentials.length === 0 && (
                  <p className="text-sm text-muted-foreground py-4">
                    No saved connections. Open Settings → LLM, choose a provider, add labeled keys, then return here.
                  </p>
                )}
                {!cfgLoading &&
                  !cfgError &&
                  agentCfg &&
                  useCredUi &&
                  selectableCredentials.length > 0 &&
                  agentCfg.roleIds.map((roleId) => {
                    const spec = roleForm[roleId];
                    if (!spec) return null;
                    const keysThisVendor = selectableCredentials.filter((g) => g.providerId === spec.provider);
                    const credGroup =
                      keysThisVendor.find((g) => g.credentialId === spec.credentialId) ?? keysThisVendor[0];
                    const group = credGroup;
                    const keySelectValue = keysThisVendor.some((k) => k.credentialId === spec.credentialId)
                      ? spec.credentialId
                      : (keysThisVendor[0]?.credentialId ?? '');
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
                            <Label className="text-xs">Vendor</Label>
                            <Select value={spec.provider} onValueChange={(v) => onVendorChange(roleId, v)}>
                              <SelectTrigger className="h-9">
                                <SelectValue placeholder="Vendor" />
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
                            ) : (
                              <Select
                                value={keySelectValue}
                                onValueChange={(v) => onCredentialPick(roleId, v)}
                              >
                                <SelectTrigger className="h-9">
                                  <SelectValue placeholder="Key" />
                                </SelectTrigger>
                                <SelectContent>
                                  {keysThisVendor.map((g) => (
                                    <SelectItem key={g.credentialId} value={g.credentialId}>
                                      {g.shortLabel ?? g.label}
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
                  !useCredUi &&
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
                    (useCredUi ? selectableCredentials.length === 0 : selectableProviders.length === 0)
                  }
                  onClick={() => void saveAgentModels()}
                >
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : 'Save'}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
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
