import { useState, useCallback, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, Save, RefreshCw, AlertCircle, Palette, Sun, Moon } from 'lucide-react';
import { useTheme, type Skin } from '@/hooks/useTheme';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type {
  LlmSettingsCatalogItem,
  LlmSettingsResponse,
  LlmSettingsPayload,
  LlmCredentialActionPayload,
  RepoSettingsResponse,
  RepoSettingsPayload,
} from '@/types';

async function fetchLlmSettings(): Promise<LlmSettingsResponse> {
  const res = await fetch('/api/settings/llm');
  if (!res.ok) throw new Error(`Settings fetch failed: ${res.status}`);
  return res.json();
}

async function saveLlmSettings(payload: LlmSettingsPayload): Promise<{ ok: boolean; restartRequired: boolean }> {
  const res = await fetch('/api/settings/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error || `Save failed: ${res.status}`);
  return data;
}

async function llmCredentialAction(payload: LlmCredentialActionPayload): Promise<{ ok: boolean; restartRequired?: boolean }> {
  const res = await fetch('/api/settings/llm/credentials', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error || `Request failed: ${res.status}`);
  return data;
}

async function refreshOAuth(): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch('/api/settings/llm/oauth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error || `Refresh failed: ${res.status}`);
  return data;
}

async function fetchRepoSettings(): Promise<RepoSettingsResponse> {
  const res = await fetch('/api/settings/repos');
  if (!res.ok) throw new Error(`Repo settings fetch failed: ${res.status}`);
  return res.json();
}

async function saveRepoSettings(payload: RepoSettingsPayload): Promise<{ ok: boolean; restartRequired: boolean }> {
  const res = await fetch('/api/settings/repos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data?.error || `Save failed: ${res.status}`);
  return data;
}

const SKINS: { id: Skin; name: string; description: string }[] = [
  { id: 'default', name: 'Default', description: 'Modern glass-morphism with blue accents' },
  {
    id: 'retro',
    name: 'Retro',
    description: 'Pixel / CRT-inspired: navy panels, neon orange/purple/teal accents, gold active nav',
  },
];

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const { theme, skin, toggleTheme, setSkin } = useTheme();
  const { data, isLoading, error } = useQuery<LlmSettingsResponse>({
    queryKey: ['settings', 'llm'],
    queryFn: fetchLlmSettings,
    staleTime: 30_000,
  });

  const [form, setForm] = useState<Partial<LlmSettingsPayload>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshSuccess, setRefreshSuccess] = useState(false);
  const [credBusy, setCredBusy] = useState<string | null>(null);
  const [credMsg, setCredMsg] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState<Record<string, string>>({});

  const saveMutation = useMutation({
    mutationFn: saveLlmSettings,
    onSuccess: () => {
      setSaveError(null);
      setSaveSuccess(true);
      queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] });
      setTimeout(() => setSaveSuccess(false), 3000);
    },
    onError: (e: Error) => {
      setSaveError(e.message);
    },
  });

  const refreshMutation = useMutation({
    mutationFn: refreshOAuth,
    onSuccess: () => {
      setRefreshError(null);
      setRefreshSuccess(true);
      queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] });
      setTimeout(() => setRefreshSuccess(false), 3000);
    },
    onError: (e: Error) => {
      setRefreshError(e.message);
    },
  });

  const {
    data: repoData,
    isLoading: repoIsLoading,
    error: repoError,
  } = useQuery<RepoSettingsResponse>({
    queryKey: ['settings', 'repos'],
    queryFn: fetchRepoSettings,
    staleTime: 30_000,
  });

  const [repoForm, setRepoForm] = useState<Partial<RepoSettingsPayload>>({});
  const [repoSaveError, setRepoSaveError] = useState<string | null>(null);
  const [repoSaveSuccess, setRepoSaveSuccess] = useState(false);

  const saveRepoMutation = useMutation({
    mutationFn: saveRepoSettings,
    onSuccess: () => {
      setRepoSaveError(null);
      setRepoSaveSuccess(true);
      queryClient.invalidateQueries({ queryKey: ['settings', 'repos'] });
      setTimeout(() => setRepoSaveSuccess(false), 3000);
    },
    onError: (e: Error) => {
      setRepoSaveError(e.message);
    },
  });

  const effectiveSettings = { ...data?.settings, ...form };
  const providerId = effectiveSettings.provider ?? 'ollama';
  const catalog = data?.catalog ?? [];
  const providerCatalog = catalog.find((p: LlmSettingsCatalogItem) => p.id === providerId);
  const models = providerCatalog?.models ?? [];
  const supportsOAuth = providerId === 'openai' && providerCatalog?.authMode === 'api_key_or_oauth';
  const showRouteSection =
    providerId === 'ollama' || providerId === 'openai_compatible' || (providerId === 'openai' && effectiveSettings.routeType === 'network');

  const providerName = providerCatalog?.name ?? providerId;
  const currentModelName =
    models.find((m) => m.id === (effectiveSettings.model ?? ''))?.name ?? effectiveSettings.model ?? '';

  const credentialsForProvider = useMemo(
    () => (data?.credentials ?? []).filter((c) => c.provider === providerId),
    [data?.credentials, providerId],
  );

  const updateForm = useCallback((updates: Partial<LlmSettingsPayload>) => {
    setForm((prev) => ({ ...prev, ...updates }));
  }, []);

  const handleSave = () => {
    setSaveError(null);
    const payload: LlmSettingsPayload = {
      provider: effectiveSettings.provider ?? 'ollama',
      model: effectiveSettings.model ?? 'llama3.2',
      authMode: effectiveSettings.authMode ?? 'api_key',
      routeType: effectiveSettings.routeType ?? 'local',
      host: effectiveSettings.host ?? '127.0.0.1',
      port: effectiveSettings.port ?? undefined,
      basePath: effectiveSettings.basePath ?? undefined,
      baseUrl: effectiveSettings.baseUrl ?? undefined,
      apiKey: effectiveSettings.authMode === 'oauth' ? '' : (form.apiKey ?? effectiveSettings.apiKey ?? ''),
      oauthStateFile: effectiveSettings.oauthStateFile,
      oauthTokenUrl: effectiveSettings.oauthTokenUrl,
      credentialLabel: form.credentialLabel ?? effectiveSettings.credentialLabel ?? undefined,
      credentialId:
        form.credentialId === ''
          ? undefined
          : (form.credentialId !== undefined ? form.credentialId : effectiveSettings.credentialId) || undefined,
    };
    if (payload.apiKey === '***') delete (payload as Record<string, unknown>).apiKey;
    saveMutation.mutate(payload);
  };

  const runCredAction = async (payload: LlmCredentialActionPayload, okMessage: string) => {
    setCredMsg(null);
    setCredBusy(payload.action + (payload.id || ''));
    try {
      await llmCredentialAction(payload);
      setCredMsg(okMessage);
      await queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] });
      setTimeout(() => setCredMsg(null), 4000);
    } catch (e) {
      setCredMsg(e instanceof Error ? e.message : 'Credential action failed');
    } finally {
      setCredBusy(null);
    }
  };

  const effectiveRepo = { ...repoData?.settings, ...repoForm };
  // Tokens are masked as "***" by the backend when configured.
  const hasGhToken = Boolean(effectiveRepo.ghToken);
  const hasAdoToken = Boolean(effectiveRepo.adoToken);
  const handleSaveRepos = () => {
    setRepoSaveError(null);
    const payload: RepoSettingsPayload = {
      ghToken: repoForm.ghToken ?? effectiveRepo.ghToken ?? '',
      adoToken: repoForm.adoToken ?? effectiveRepo.adoToken ?? '',
      adoOrgUrl: repoForm.adoOrgUrl ?? effectiveRepo.adoOrgUrl ?? '',
    };
    if (payload.ghToken === '***') delete (payload as Record<string, unknown>).ghToken;
    if (payload.adoToken === '***') delete (payload as Record<string, unknown>).adoToken;
    if (payload.adoOrgUrl === '***') delete (payload as Record<string, unknown>).adoOrgUrl;
    saveRepoMutation.mutate(payload);
  };

  if (isLoading || error) {
    return (
      <div className="p-6 lg:p-8 max-w-[800px] mx-auto space-y-6">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Settings</h1>
        {isLoading && (
          <div className="flex items-center gap-2 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading settings…
          </div>
        )}
        {error && (
          <div className="flex items-center gap-2 text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            {String(error)}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="p-6 lg:p-8 max-w-[800px] mx-auto space-y-8">
      <h1 className="text-2xl font-bold tracking-tight text-foreground">Settings</h1>
      <p className="text-sm text-muted-foreground">Configure LLM providers, models, and authentication.</p>

      <div className="glass-card p-6">
        <Accordion type="single" collapsible defaultValue="appearance">
          <AccordionItem value="appearance">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span className="flex items-center gap-2">
                  <Palette className="h-4 w-4" />
                  Appearance
                </span>
                <span className="text-xs text-muted-foreground">
                  {SKINS.find((s) => s.id === skin)?.name ?? skin} · {theme === 'dark' ? 'Dark' : 'Light'}
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-6 pt-4">
                <div className="space-y-2">
                  <Label>Skin</Label>
                  <Select value={skin} onValueChange={(v: Skin) => setSkin(v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SKINS.map((s) => (
                        <SelectItem key={s.id} value={s.id}>
                          <div>
                            <div className="font-medium">{s.name}</div>
                            <div className="text-xs text-muted-foreground">{s.description}</div>
                          </div>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>Theme</Label>
                  <div className="flex gap-2">
                    <Button
                      variant={theme === 'dark' ? 'default' : 'outline'}
                      size="sm"
                      onClick={() => theme !== 'dark' && toggleTheme()}
                    >
                      <Moon className="h-4 w-4 mr-1" />
                      Dark
                    </Button>
                    <Button
                      variant={theme === 'light' ? 'default' : 'outline'}
                      size="sm"
                      onClick={() => theme !== 'light' && toggleTheme()}
                    >
                      <Sun className="h-4 w-4 mr-1" />
                      Light
                    </Button>
                  </div>
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="llm-provider">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span>LLM Provider</span>
                <span className="text-xs text-muted-foreground">
                  {providerName}
                  {currentModelName ? ` / ${currentModelName}` : ''}
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-6 pt-4">
                <div className="space-y-2">
                  <Label>Provider</Label>
                  <Select
                    value={providerId}
                    onValueChange={(v) => {
                      const p = catalog.find((x: LlmSettingsCatalogItem) => x.id === v);
                      // Default to API key whenever provider changes to avoid OAuth getting "stuck".
                      updateForm({ provider: v, authMode: 'api_key' });
                      if (p?.models?.length) updateForm({ model: p.models[0].id });
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {catalog.map((p: LlmSettingsCatalogItem) => (
                        <SelectItem key={p.id} value={p.id}>
                          {p.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className="space-y-2">
                  <Label>Model</Label>
                  {models.length > 0 ? (
                    <Select
                      value={effectiveSettings.model ?? ''}
                      onValueChange={(v) => updateForm({ model: v })}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select model" />
                      </SelectTrigger>
                      <SelectContent>
                        {models.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {m.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <Input
                      placeholder="Enter model ID (e.g. gpt-4o)"
                      value={form.model ?? effectiveSettings.model ?? ''}
                      onChange={(e) => updateForm({ model: e.target.value })}
                    />
                  )}
                </div>

                {providerId === 'openai_compatible' && (
                  <div className="space-y-2">
                    <Label>Base URL</Label>
                    <Input
                      placeholder="https://api.example.com/v1"
                      value={form.baseUrl ?? effectiveSettings.baseUrl ?? ''}
                      onChange={(e) => updateForm({ baseUrl: e.target.value })}
                    />
                  </div>
                )}

                {showRouteSection && (
                  <>
                    <h3 className="text-sm font-medium pt-2 border-t">Route</h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div className="space-y-2">
                        <Label>Type</Label>
                        <Select
                          value={effectiveSettings.routeType ?? 'local'}
                          onValueChange={(v: 'local' | 'network') => updateForm({ routeType: v })}
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="local">Local (this machine)</SelectItem>
                            <SelectItem value="network">Network</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-2">
                        <Label>Host</Label>
                        <Input
                          placeholder="127.0.0.1"
                          value={form.host ?? effectiveSettings.host ?? '127.0.0.1'}
                          onChange={(e) => updateForm({ host: e.target.value })}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>Port (optional)</Label>
                        <Input
                          type="number"
                          placeholder="11434"
                          value={form.port ?? effectiveSettings.port ?? ''}
                          onChange={(e) => {
                            const v = e.target.value;
                            updateForm({ port: v ? parseInt(v, 10) : undefined });
                          }}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>Base path (optional)</Label>
                        <Input
                          placeholder="/v1"
                          value={form.basePath ?? effectiveSettings.basePath ?? ''}
                          onChange={(e) => updateForm({ basePath: e.target.value })}
                        />
                      </div>
                    </div>
                  </>
                )}

                <h3 className="text-sm font-medium pt-2 border-t">Authentication</h3>
                {providerId !== 'ollama' && (
                  <div className="space-y-2 rounded-lg border border-border/50 bg-muted/20 p-3">
                    <Label className="text-xs text-muted-foreground">
                      Saved API keys for {providerName}
                    </Label>
                    <p className="text-[11px] text-muted-foreground leading-snug">
                      Only keys for the <strong>selected provider</strong> are listed. Each label must be unique per
                      vendor. Keys live in{' '}
                      <code className="text-[10px]">worker-manager/llm_credentials.json</code>.{' '}
                      <strong>Use</strong> copies a key into the active profile (LLM_*).
                    </p>
                    {credentialsForProvider.length === 0 && (
                      <p className="text-xs text-muted-foreground italic py-1">
                        No saved keys for this provider yet. Add a label and API key below, then Save.
                      </p>
                    )}
                    <ul className="space-y-2">
                      {credentialsForProvider.map((c) => (
                        <li
                          key={c.id}
                          className="flex flex-col gap-2 rounded-md border border-border/40 bg-background/60 p-2 sm:flex-row sm:flex-wrap sm:items-center sm:gap-2"
                        >
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0 flex-1">
                            <span className="font-medium text-sm truncate">{c.label}</span>
                            <span className="font-mono text-[11px] text-muted-foreground">
                              {c.hasKey ? `···${c.keySuffix || '••••'}` : 'empty'}
                            </span>
                            {data?.activeCredentialId === c.id && (
                              <span className="text-[10px] font-medium uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
                                Active
                              </span>
                            )}
                          </div>
                          <div className="flex flex-wrap gap-1 shrink-0">
                            <Button
                              type="button"
                              size="sm"
                              variant="secondary"
                              className="h-8"
                              disabled={!!credBusy}
                              onClick={() =>
                                void runCredAction({ action: 'activate', id: c.id }, 'Active profile updated.')
                              }
                            >
                              Use
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-8"
                              disabled={!!credBusy}
                              onClick={() => {
                                updateForm({ credentialId: c.id, credentialLabel: c.label });
                                setCredMsg(`Editing "${c.label}" — enter a new key below and Save, or rename here.`);
                              }}
                            >
                              Edit
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              className="h-8 text-destructive hover:text-destructive"
                              disabled={!!credBusy}
                              onClick={() => {
                                if (!window.confirm(`Delete saved key "${c.label}"?`)) return;
                                void runCredAction({ action: 'delete', id: c.id }, 'Credential removed.');
                              }}
                            >
                              Delete
                            </Button>
                          </div>
                          <div className="flex w-full flex-col gap-1 sm:flex-row sm:items-center">
                            <Input
                              className="h-8 text-sm flex-1"
                              placeholder="Rename…"
                              value={renameDraft[c.id] ?? ''}
                              onChange={(e) => setRenameDraft((prev) => ({ ...prev, [c.id]: e.target.value }))}
                            />
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-8 shrink-0"
                              disabled={!!credBusy || !(renameDraft[c.id] ?? '').trim()}
                              onClick={() => {
                                const name = (renameDraft[c.id] ?? '').trim();
                                if (!name) return;
                                void runCredAction(
                                  { action: 'patch', id: c.id, label: name },
                                  'Label updated.',
                                ).then(() =>
                                  setRenameDraft((prev) => {
                                    const next = { ...prev };
                                    delete next[c.id];
                                    return next;
                                  }),
                                );
                              }}
                            >
                              Save label
                            </Button>
                          </div>
                        </li>
                      ))}
                    </ul>
                    {credMsg && <p className="text-xs text-muted-foreground">{credMsg}</p>}
                  </div>
                )}
                {supportsOAuth ? (
                  <div className="space-y-2">
                    <Label>Auth mode</Label>
                    <Select
                      value={effectiveSettings.authMode ?? 'api_key'}
                      onValueChange={(v: 'api_key' | 'oauth') => updateForm({ authMode: v })}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="api_key">API Key</SelectItem>
                        <SelectItem value="oauth">OAuth (Codex / OpenAI)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                ) : null}

                {effectiveSettings.authMode === 'api_key' && providerId !== 'ollama' && (
                  <div className="space-y-3">
                    <div className="space-y-2">
                      <Label>Key label</Label>
                      <Input
                        placeholder="e.g. Work Gemini, Personal OpenAI"
                        value={form.credentialLabel ?? effectiveSettings.credentialLabel ?? ''}
                        onChange={(e) => updateForm({ credentialLabel: e.target.value })}
                      />
                      <p className="text-[11px] text-muted-foreground">
                        Unique among all <strong>{providerName}</strong> keys. When you paste a new API key and Save, it
                        is stored for this provider and becomes the active profile.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label>API Key</Label>
                      {effectiveSettings.apiKey === '***' && (
                        <p className="text-xs text-emerald-600 dark:text-emerald-400">
                          Key is saved
                          {effectiveSettings.keySuffix ? ` (ends with ···{effectiveSettings.keySuffix})` : ''}.
                          Paste a new key only if you want to replace it.
                        </p>
                      )}
                      <Input
                        type="password"
                        placeholder={effectiveSettings.apiKey === '***' ? 'Leave blank to keep saved key' : 'sk-… or paste key'}
                        value={form.apiKey ?? (effectiveSettings.apiKey === '***' ? '' : effectiveSettings.apiKey ?? '')}
                        onChange={(e) => updateForm({ apiKey: e.target.value })}
                      />
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => updateForm({ credentialId: '', credentialLabel: '' })}
                    >
                      New credential (clear selection)
                    </Button>
                  </div>
                )}

                {effectiveSettings.authMode === 'oauth' && providerId === 'openai' && (
                  <div className="space-y-4 p-4 rounded-lg bg-muted/50">
                    <p className="text-xs text-muted-foreground leading-relaxed">
                      <strong>ChatGPT / Codex OAuth</strong> — good for Codex-style sessions.{' '}
                      <strong className="text-foreground">Plan New Work and hosted GPT via api.openai.com</strong> need an
                      OpenAI <strong>platform API key</strong> (<code className="text-[11px]">sk-…</code>): switch{' '}
                      <strong>Auth mode</strong> to <strong>API Key</strong> or add the key from{' '}
                      <span className="whitespace-nowrap">platform.openai.com/api-keys</span>. Codex browser OAuth tokens
                      do not receive <code className="text-[11px]">model.request</code> on authorize, but{' '}
                      <code className="text-[11px]">/v1/chat/completions</code> still requires it — so OAuth alone often
                      cannot run the planner. Optional: <code className="text-[11px]">./flume codex-oauth login-paste</code>{' '}
                      / <code className="text-[11px]">login-browser</code> / Codex import for other uses; then{' '}
                      <code className="text-[11px]">./flume restart --all</code>.
                      <span className="block mt-1">
                        <strong>Refresh token</strong> only renews the same consent — it cannot add API product scopes
                        OpenAI did not grant.
                      </span>
                    </p>
                    <div className="space-y-2">
                      <Label>OAuth state file</Label>
                      <Input
                        placeholder=".openai-oauth.json"
                        value={form.oauthStateFile ?? effectiveSettings.oauthStateFile ?? ''}
                        onChange={(e) => updateForm({ oauthStateFile: e.target.value })}
                      />
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => refreshMutation.mutate()}
                        disabled={refreshMutation.isPending || !data?.oauthStatus?.configured}
                      >
                        {refreshMutation.isPending ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <RefreshCw className="h-4 w-4" />
                        )}
                        Refresh token
                      </Button>
                      {data?.oauthStatus?.configured && (
                        <span className="text-xs text-muted-foreground">
                          Expires in {data.oauthStatus.expiresInSeconds}s
                        </span>
                      )}
                    </div>
                    {data?.oauthStatus?.configured && (
                      <div className="text-xs space-y-2">
                        {data.oauthStatus.oauthScopeStatus === 'ok' && (
                          <p className="text-green-700 dark:text-green-500">
                            OAuth scopes look OK for <code className="text-[11px]">api.responses.write</code>.
                          </p>
                        )}
                        {data.oauthStatus.oauthScopeStatus === 'no_token' && (
                          <p className="text-amber-600 dark:text-amber-500">
                            No access token in the OAuth state file. Click <strong>Refresh token</strong> or complete
                            login again.
                          </p>
                        )}
                        {data.oauthStatus.oauthScopeStatus === 'opaque_or_unknown' &&
                          data.oauthStatus.hasAccessToken && (
                            <p className="text-amber-600 dark:text-amber-500">
                              Access token is not a JWT we can decode (or parsing failed), so scopes are unknown here.
                              If API calls return 401 “Missing scopes: api.responses.write”, run{' '}
                              <code className="text-[11px]">./flume codex-oauth login-browser</code> or import from Codex,
                              then <code className="text-[11px]">./flume restart --all</code>.
                            </p>
                          )}
                        {data.oauthStatus.oauthScopeStatus === 'jwt_no_scp' && (
                          <p className="text-amber-600 dark:text-amber-500">
                            JWT decodes but has no <code className="text-[11px]">scp</code> / roles we can read. If you
                            still get 401 on <code className="text-[11px]">/v1/responses</code>, re-consent via{' '}
                            <code className="text-[11px]">login-browser</code> or Codex import.
                          </p>
                        )}
                        {data.oauthStatus.oauthScopeStatus === 'missing_responses_write' && (
                          <p className="text-amber-700 dark:text-amber-400">
                            Typical <strong>Codex OAuth</strong> JWT (connector scopes only). Flume routes to{' '}
                            <code className="text-[11px]">/v1/chat/completions</code>, but OpenAI usually still requires{' '}
                            <code className="text-[11px]">model.request</code> — not granted by Codex authorize. For{' '}
                            <strong>Plan New Work</strong>, use a platform <code className="text-[11px]">sk-</code> API key
                            (Auth mode → API Key).
                          </p>
                        )}
                        {data.oauthStatus.accessTokenAudience ? (
                          <p className="text-muted-foreground">
                            <span className="font-medium">Audience</span>{' '}
                            <code className="break-all text-[11px]">{data.oauthStatus.accessTokenAudience}</code>
                          </p>
                        ) : null}
                        {Array.isArray(data.oauthStatus.accessTokenScopes) &&
                        data.oauthStatus.accessTokenScopes.length > 0 ? (
                          <div className="space-y-1">
                            <p className="text-muted-foreground font-medium">Access token scopes (from JWT)</p>
                            <p className="font-mono break-all text-[11px]">
                              {data.oauthStatus.accessTokenScopes.join(' ')}
                            </p>
                          </div>
                        ) : null}
                        {data.oauthStatus.oauthScopesRequested ? (
                          <p className="text-muted-foreground">
                            <span className="font-medium">Scopes requested at login</span>{' '}
                            <code className="break-all text-[11px]">{data.oauthStatus.oauthScopesRequested}</code>
                          </p>
                        ) : null}
                      </div>
                    )}
                    {refreshError && (
                      <p className="text-sm text-destructive">{refreshError}</p>
                    )}
                    {refreshSuccess && (
                      <div className="space-y-1">
                        <p className="text-sm text-green-600">Token refreshed successfully.</p>
                        {data?.oauthStatus?.oauthScopeStatus &&
                          data.oauthStatus.oauthScopeStatus !== 'ok' && (
                            <p className="text-sm text-amber-600 dark:text-amber-500">
                              If scopes were wrong before, they are still wrong — refresh does not add consent. Use{' '}
                              <code className="text-[11px]">login-browser</code> or Codex import, then restart services.
                            </p>
                          )}
                      </div>
                    )}
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-3 pt-4">
                  <Button onClick={handleSave} disabled={saveMutation.isPending}>
                    {saveMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4" />
                    )}
                    Save
                  </Button>
                  {saveError && (
                    <span className="text-sm text-destructive">{saveError}</span>
                  )}
                  {saveSuccess && (
                    <span className="text-sm text-green-600">Saved. Restart dashboard and workers to apply.</span>
                  )}
                </div>

                {data?.restartRequired && (
                  <p className="text-xs text-muted-foreground">
                    Restart the dashboard and worker manager for changes to take effect.
                  </p>
                )}
                {data?.openbaoInstalled === false && (
                  <p className="text-xs text-destructive">
                    OpenBao is not installed. Sensitive settings will be stored in an insecure local <code>.env</code> file.
                  </p>
                )}
              </div>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="repo-tokens">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span>Repo Credentials</span>
                <span className="text-xs text-muted-foreground">
                  {hasGhToken ? '✓' : ''}
                  {hasAdoToken ? (hasGhToken ? ' / ADO ✓' : 'ADO ✓') : ''}
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-6 pt-4">
                {repoIsLoading && (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Loading repo settings…
                  </div>
                )}
                {repoError && (
                  <p className="text-sm text-destructive">{String(repoError)}</p>
                )}

                <div className="space-y-2">
                  <Label>GitHub token</Label>
                  <Input
                    type="password"
                    placeholder={effectiveRepo.ghToken === '***' ? '••••••••' : 'ghp_...'}
                    value={repoForm.ghToken ?? (effectiveRepo.ghToken === '***' ? '' : effectiveRepo.ghToken ?? '')}
                    onChange={(e) => setRepoForm((prev) => ({ ...prev, ghToken: e.target.value }))}
                  />
                </div>

                <div className="space-y-2">
                  <Label>Azure DevOps (ADO) PAT</Label>
                  <Input
                    type="password"
                    placeholder={effectiveRepo.adoToken === '***' ? '••••••••' : 'ado_pat_...'}
                    value={repoForm.adoToken ?? (effectiveRepo.adoToken === '***' ? '' : effectiveRepo.adoToken ?? '')}
                    onChange={(e) => setRepoForm((prev) => ({ ...prev, adoToken: e.target.value }))}
                  />
                </div>

                <div className="space-y-2">
                  <Label>ADO Org URL (optional)</Label>
                  <Input
                    placeholder="https://dev.azure.com/<org>"
                    value={repoForm.adoOrgUrl ?? effectiveRepo.adoOrgUrl ?? ''}
                    onChange={(e) => setRepoForm((prev) => ({ ...prev, adoOrgUrl: e.target.value }))}
                  />
                </div>

                <div className="flex flex-wrap items-center gap-3 pt-4">
                  <Button onClick={handleSaveRepos} disabled={saveRepoMutation.isPending}>
                    {saveRepoMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4" />
                    )}
                    Save
                  </Button>
                  {repoSaveError && <span className="text-sm text-destructive">{repoSaveError}</span>}
                  {repoSaveSuccess && <span className="text-sm text-green-600">Saved. Restart dashboard and workers to apply.</span>}
                </div>

                {repoData?.restartRequired && (
                  <p className="text-xs text-muted-foreground">
                    Restart the dashboard and worker manager for changes to take effect.
                  </p>
                )}
                {data?.openbaoInstalled === false && (
                  <p className="text-xs text-destructive">
                    OpenBao is not installed. Sensitive settings will be stored in an insecure local <code>.env</code> file.
                  </p>
                )}
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}
