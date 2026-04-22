import { useState, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Loader2,
  Save,
  RefreshCw,
  AlertCircle,
  Palette,
  Sun,
  Moon,
  RotateCcw,
  Terminal,
  Plus,
  Trash2,
  Star,
} from 'lucide-react';
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
import { Switch } from '@/components/ui/switch';
import type {
  LlmSettingsCatalogItem,
  LlmSettingsResponse,
  LlmSettingsPayload,
  LlmCredentialActionPayload,
  CodexAppServerStatusResponse,
  RepoSettingsResponse,
  RepoSettingsPayload,
  GithubTokenActionPayload,
  AdoTokenActionPayload,
} from '@/types';

interface SystemSettingsPayload {
  es_url: string;
  es_api_key: string;
  openbao_url: string;
  vault_token: string;
  prometheus_enabled?: boolean;
}

async function fetchSystemSettings(): Promise<SystemSettingsPayload> {
  const res = await fetch('/api/settings/system');
  const data = await parseJsonBody<SystemSettingsPayload & { error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Settings fetch failed: ${res.status}`);
  return data;
}

async function saveSystemSettings(payload: SystemSettingsPayload): Promise<{ status: string }> {
  const res = await fetch('/api/settings/system', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await parseJsonBody<{ status?: string; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Save failed: ${res.status}`);
  return data as { status: string };
}

/** Avoid `res.json()` on empty bodies (e.g. legacy 404s, proxies, dropped connections). */
async function parseJsonBody<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!text.trim()) {
    if (!res.ok) {
      if (res.status === 404) {
        throw new Error(
          'HTTP 404 with an empty response usually means the browser did not reach this Flume dashboard Python server ' +
            '(wrong URL/port, or a proxy in front returned 404), or the dashboard is still running an old server.py. ' +
            'Use the same host/port as the UI (e.g. :8765), restart the dashboard from your current checkout, and hard-refresh.',
        );
      }
      throw new Error(`Empty response body (HTTP ${res.status}).`);
    }
    return {} as T;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(
      `Invalid JSON from server (HTTP ${res.status}): ${text.slice(0, 160)}${text.length > 160 ? '…' : ''}`,
    );
  }
}

async function fetchLlmSettings(): Promise<LlmSettingsResponse> {
  const res = await fetch('/api/settings/llm');
  const data = await parseJsonBody<LlmSettingsResponse & { error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Settings fetch failed: ${res.status}`);
  return data;
}

async function saveLlmSettings(payload: LlmSettingsPayload): Promise<{ ok: boolean }> {
  const res = await fetch('/api/settings/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await parseJsonBody<{ ok?: boolean; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Save failed: ${res.status}`);
  return data as { ok: boolean };
}

async function llmCredentialAction(payload: LlmCredentialActionPayload): Promise<{ ok: boolean }> {
  const res = await fetch('/api/settings/llm/credentials', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await parseJsonBody<{ ok?: boolean; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Request failed: ${res.status}`);
  return data as { ok: boolean };
}

async function fetchCodexAppServerStatus(): Promise<CodexAppServerStatusResponse> {
  const res = await fetch('/api/codex-app-server/status');
  if (!res.ok) throw new Error(`Codex app-server status failed: ${res.status}`);
  return res.json();
}

async function refreshOAuth(): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch('/api/settings/llm/oauth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const data = await parseJsonBody<{ ok?: boolean; message?: string; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Refresh failed: ${res.status}`);
  return data as { ok: boolean; message?: string };
}

async function fetchRepoSettings(): Promise<RepoSettingsResponse> {
  const res = await fetch('/api/settings/repos');
  const data = await parseJsonBody<RepoSettingsResponse & { error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Repo settings fetch failed: ${res.status}`);
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

  // Must be declared before any early return to satisfy React's Rules of Hooks.
  const [userPerspective, setUserPerspective] = useState<string>(() => {
    try {
      return localStorage.getItem('fremen-user-perspective') ?? 'standard';
    } catch {
      return 'standard';
    }
  });

  const onPerspectiveChange = (v: string) => {
    setUserPerspective(v);
    try {
      localStorage.setItem('fremen-user-perspective', v);
    } catch {
      /* ignore */
    }
  };

  const saveMutation = useMutation({
    mutationFn: saveLlmSettings,
    onSuccess: (data) => {
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
    onSuccess: (data) => {
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

  const {
    data: codexAppData,
    isLoading: codexAppLoading,
    error: codexAppError,
    refetch: refetchCodexApp,
  } = useQuery<CodexAppServerStatusResponse>({
    queryKey: ['settings', 'codex-app-server'],
    queryFn: fetchCodexAppServerStatus,
    staleTime: 15_000,
  });

  const [repoSaveError, setRepoSaveError] = useState<string | null>(null);
  const [repoSaveSuccess, setRepoSaveSuccess] = useState(false);

  const {
    data: sysData,
    isLoading: sysIsLoading,
    error: sysError,
  } = useQuery<SystemSettingsPayload>({
    queryKey: ['settings', 'system'],
    queryFn: fetchSystemSettings,
    staleTime: 30_000,
  });

  const { data: exoData } = useQuery<{ active: boolean; baseUrl?: string }>({
    queryKey: ['settings', 'exo-status'],
    queryFn: async () => {
      const res = await fetch('/api/exo-status');
      if (!res.ok) {
        throw new Error(`Failed to fetch exo-status: ${res.statusText}`);
      }
      return await res.json();
    },
    staleTime: 30_000,
  });

  const [sysForm, setSysForm] = useState<Partial<SystemSettingsPayload>>({});
  const [sysSaveError, setSysSaveError] = useState<string | null>(null);
  const [sysSaveSuccess, setSysSaveSuccess] = useState(false);

  const sysMutation = useMutation({
    mutationFn: saveSystemSettings,
    onSuccess: () => {
      setSysSaveError(null);
      setSysSaveSuccess(true);
      queryClient.invalidateQueries({ queryKey: ['settings', 'system'] });
      setTimeout(() => setSysSaveSuccess(false), 3000);

    },
    onError: (e: Error) => {
      setSysSaveError(e.message);
    },
  });

  const effectiveSys = { ...sysData, ...sysForm };



  const effectiveSettings = { ...data?.settings, ...form };
  const providerId = effectiveSettings.provider ?? 'ollama';
  const catalog = data?.catalog ?? [];
  const providerCatalog = catalog.find((p: LlmSettingsCatalogItem) => p.id === providerId);
  const models = providerCatalog?.models ?? [];
  const supportsManualModelEntry = providerId === 'ollama' || providerId === 'openai_compatible';
  const modelSuggestions = useMemo(() => {
    const out = [...models];
    const current = (effectiveSettings.model ?? '').trim();
    if (current && !out.some((m) => m.id === current)) out.unshift({ id: current, name: current });
    return out;
  }, [models, effectiveSettings.model]);
  const supportsOAuth = providerId === 'openai' && providerCatalog?.authMode === 'api_key_or_oauth';
  const showRouteSection =
    providerId === 'ollama' || providerId === 'openai_compatible' || (providerId === 'openai' && effectiveSettings.routeType === 'network');

  const providerName = providerCatalog?.name ?? providerId;
  const currentModelName =
    models.find((m) => m.id === (effectiveSettings.model ?? ''))?.name ?? effectiveSettings.model ?? '';

  const credentialsForProvider = useMemo(
    () =>
      (data?.credentials ?? []).filter(
        (c) => (c.provider || '').toLowerCase() === (providerId || '').toLowerCase(),
      ),
    [data?.credentials, providerId],
  );

  const defaultCredId = data?.defaultCredentialId ?? data?.activeCredentialId ?? '';

  /** Saved server profile (before you change the Provider dropdown). */
  const persistedProvider = data?.settings?.provider ?? 'ollama';
  /** Masked env key + active credential apply only when the form provider matches the saved profile. */
  const showPersistedMaskedApiKey =
    providerId === persistedProvider &&
    (effectiveSettings.authMode ?? 'api_key') === 'api_key' &&
    data?.settings?.apiKey === '***';

  const updateForm = useCallback((updates: Partial<LlmSettingsPayload>) => {
    setForm((prev) => ({ ...prev, ...updates }));
  }, []);

  const handleSave = () => {
    setSaveError(null);
    let credentialId: string | undefined =
      form.credentialId === ''
        ? undefined
        : (form.credentialId !== undefined ? form.credentialId : effectiveSettings.credentialId) || undefined;
    if (
      credentialId &&
      !(data?.credentials ?? []).some((c) => c.provider === (effectiveSettings.provider ?? 'ollama') && c.id === credentialId)
    ) {
      credentialId = undefined;
    }
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
      credentialId,
    };
    if (payload.apiKey === '***') delete (payload as { apiKey?: string }).apiKey;
    saveMutation.mutate(payload);
  };

  const runCredAction = async (payload: LlmCredentialActionPayload, okMessage: string) => {
    setCredMsg(null);
    setCredBusy(payload.action + (payload.id || ''));
    try {
      const credRes = await llmCredentialAction(payload);

      setCredMsg(okMessage);
      await queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] });
      setTimeout(() => setCredMsg(null), 4000);
    } catch (e) {
      setCredMsg(e instanceof Error ? e.message : 'Credential action failed');
    } finally {
      setCredBusy(null);
    }
  };

  const githubTokens = repoData?.settings?.githubTokens ?? [];
  const activeGithubId = repoData?.settings?.activeGithubTokenId ?? '';
  const hasGhToken = githubTokens.some((t) => t.hasToken);
  const adoCredentials = repoData?.settings?.adoCredentials ?? [];
  const activeAdoId = repoData?.settings?.activeAdoCredentialId ?? '';
  const hasAdoToken = adoCredentials.some((t) => t.hasToken);

  const [newGithubLabel, setNewGithubLabel] = useState('');
  const [newGithubToken, setNewGithubToken] = useState('');
  const [ghRename, setGhRename] = useState<Record<string, string>>({});
  const [ghReplaceToken, setGhReplaceToken] = useState<Record<string, string>>({});
  const [ghBusy, setGhBusy] = useState<string | null>(null);

  const [newAdoLabel, setNewAdoLabel] = useState('');
  const [newAdoOrgUrl, setNewAdoOrgUrl] = useState('');
  const [newAdoToken, setNewAdoToken] = useState('');
  const [adoRename, setAdoRename] = useState<Record<string, string>>({});
  const [adoReplaceToken, setAdoReplaceToken] = useState<Record<string, string>>({});
  const [adoReplaceOrg, setAdoReplaceOrg] = useState<Record<string, string>>({});
  const [adoBusy, setAdoBusy] = useState<string | null>(null);

  const runGithubTokenAction = async (body: GithubTokenActionPayload, busyKey: string): Promise<boolean> => {
    setGhBusy(busyKey);
    setRepoSaveError(null);
    try {
      const res = await fetch('/api/settings/repos', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ githubTokenAction: body } satisfies RepoSettingsPayload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || `Request failed: ${res.status}`);
      await queryClient.invalidateQueries({ queryKey: ['settings', 'repos'] });
      setRepoSaveSuccess(true);
      setTimeout(() => setRepoSaveSuccess(false), 3000);
      return true;
    } catch (e) {
      setRepoSaveError(e instanceof Error ? e.message : 'GitHub token action failed');
      return false;
    } finally {
      setGhBusy(null);
    }
  };

  const runAdoTokenAction = async (body: AdoTokenActionPayload, busyKey: string): Promise<boolean> => {
    setAdoBusy(busyKey);
    setRepoSaveError(null);
    try {
      const res = await fetch('/api/settings/repos', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adoTokenAction: body } satisfies RepoSettingsPayload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || `Request failed: ${res.status}`);
      await queryClient.invalidateQueries({ queryKey: ['settings', 'repos'] });
      setRepoSaveSuccess(true);
      setTimeout(() => setRepoSaveSuccess(false), 3000);
      return true;
    } catch (e) {
      setRepoSaveError(e instanceof Error ? e.message : 'ADO credential action failed');
      return false;
    } finally {
      setAdoBusy(null);
    }
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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">Settings</h1>
          <p className="text-sm text-muted-foreground">Configure LLM providers, models, and authentication.</p>
        </div>
        
        <div className="w-48">
          <Select value={userPerspective} onValueChange={onPerspectiveChange}>
            <SelectTrigger className="h-8">
              <SelectValue placeholder="Perspective" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="standard">Standard User</SelectItem>
              <SelectItem value="exo_administrator">Exo Administrator</SelectItem>
              <SelectItem value="developer">Developer</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>



      <div className="glass-card p-6">
        <Accordion type="single" collapsible>
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

          <AccordionItem value="codex-app-server">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span className="flex items-center gap-2">
                  <Terminal className="h-4 w-4" />
                  Codex app-server
                </span>
                <span className="text-xs text-muted-foreground">
                  {codexAppLoading
                    ? '…'
                    : codexAppData?.parseError
                      ? 'config'
                      : codexAppData?.tcpReachable
                        ? 'port open'
                        : codexAppData?.flumeWillUseNpxFallback
                          ? 'start: npx'
                          : 'port closed'}
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-4 pt-4 text-sm">
                <p className="text-muted-foreground">
                  Run the official <strong>Codex app-server</strong> on this host to use{' '}
                  <strong>ChatGPT/Codex OAuth</strong> for agent coding and review (JSON-RPC — not Flume&apos;s HTTP LLM
                  path). See{' '}
                  <a
                    href={codexAppData?.docsUrl ?? 'https://developers.openai.com/codex/app-server'}
                    className="text-primary underline-offset-2 hover:underline"
                    target="_blank"
                    rel="noreferrer"
                  >
                    OpenAI docs
                  </a>
                  . For an in-dashboard WebSocket client (with optional approval replies), open{' '}
                  <Link to="/codex" className="text-primary underline-offset-2 hover:underline">
                    Codex
                  </Link>{' '}
                  (requires <code className="text-[10px]">websockets</code> and the dashboard proxy — see install docs).
                </p>
                {codexAppLoading && (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Checking status…
                  </div>
                )}
                {codexAppError && (
                  <p className="text-destructive">{String(codexAppError)}</p>
                )}
                {codexAppData && (
                  <div className="space-y-2 rounded-md border border-border/60 bg-muted/30 p-4 font-mono text-[12px] leading-relaxed">
                    <div>
                      <span className="text-muted-foreground">{codexAppData.envFlumeListen}</span>={codexAppData.listenUrl}
                    </div>
                    <div>
                      <span className="text-muted-foreground">{codexAppData.envCodexBin}</span>={codexAppData.codexBinary}
                      {codexAppData.codexResolvedPath ? (
                        <span className="text-muted-foreground"> ({codexAppData.codexResolvedPath})</span>
                      ) : null}
                    </div>
                    <div>
                      <span className="font-medium text-foreground">codex on PATH:</span>{' '}
                      {codexAppData.codexOnPath ? 'yes' : 'no'}
                    </div>
                    <div>
                      <span className="font-medium text-foreground">npx on PATH:</span>{' '}
                      {codexAppData.npxOnPath ? `yes${codexAppData.npxResolvedPath ? ` (${codexAppData.npxResolvedPath})` : ''}` : 'no'}
                    </div>
                    {codexAppData.flumeWillUseNpxFallback ? (
                      <p className="text-emerald-700 dark:text-emerald-400 font-sans text-[11px]">
                        <strong>./flume codex-app-server</strong> will run{' '}
                        <code className="text-[10px]">npx --yes @openai/codex app-server …</code> (no global{' '}
                        <code className="text-[10px]">codex</code> required).
                      </p>
                    ) : null}
                    <div>
                      <span className="font-medium text-foreground">~/.codex/auth.json:</span>{' '}
                      {codexAppData.codexAuthFilePresent ? 'present' : 'missing'}
                    </div>
                    {codexAppData.parseError ? (
                      <p className="text-amber-700 dark:text-amber-400">{codexAppData.parseError}</p>
                    ) : (
                      <div>
                        <span className="font-medium text-foreground">TCP (listen port):</span>{' '}
                        {codexAppData.tcpReachable ? (
                          <span className="text-green-600 dark:text-green-400">reachable</span>
                        ) : (
                          <span className="text-amber-700 dark:text-amber-400">not listening (start ./flume codex-app-server)</span>
                        )}
                      </div>
                    )}
                    <p className="text-muted-foreground pt-2 font-sans text-[11px]">
                      TCP check only — it does not validate JSON-RPC. Default listen is {codexAppData.defaultListenUrl}.
                    </p>
                  </div>
                )}
                <Button type="button" variant="outline" size="sm" onClick={() => refetchCodexApp()}>
                  <RefreshCw className="h-4 w-4 mr-1" />
                  Recheck
                </Button>
              </div>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="repo-tokens">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span>Repo credentials</span>
                <span className="text-xs text-muted-foreground">
                  {hasGhToken ? 'GitHub classic ✓' : ''}
                  {hasAdoToken ? (hasGhToken ? ' · ADO ✓' : 'ADO ✓') : ''}
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

                <div className="space-y-4">
                  <div>
                    <Label>GitHub tokens</Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Add labeled PATs; the <strong>active</strong> one is written to <code className="text-[10px]">GH_TOKEN</code>{' '}
                      for clones and tooling. Only one is active at a time.
                    </p>
                  </div>

                  <div className="rounded-md border border-border/60 bg-muted/20 p-4 space-y-3">
                    <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Add PAT</div>
                    <div className="grid gap-2 sm:grid-cols-2">
                      <Input
                        placeholder="Label (e.g. work, personal)"
                        value={newGithubLabel}
                        onChange={(e) => setNewGithubLabel(e.target.value)}
                        autoComplete="off"
                      />
                      <Input
                        type="password"
                        placeholder="ghp_…"
                        value={newGithubToken}
                        onChange={(e) => setNewGithubToken(e.target.value)}
                        autoComplete="new-password"
                      />
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      disabled={!!ghBusy}
                      onClick={() => {
                        const label = newGithubLabel.trim();
                        const token = newGithubToken.trim();
                        if (!label || !token) {
                          setRepoSaveError('Label and token are required to add a GitHub PAT.');
                          return;
                        }
                        void runGithubTokenAction({ action: 'upsert', label, token }, 'add').then((ok) => {
                          if (ok) {
                            setNewGithubLabel('');
                            setNewGithubToken('');
                          }
                        });
                      }}
                    >
                      <Plus className="h-4 w-4 mr-1" />
                      Add token
                    </Button>
                  </div>

                  {githubTokens.length > 0 && (
                    <ul className="space-y-4">
                      {githubTokens.map((t) => {
                        const isActive = t.id === activeGithubId;
                        const renameVal = ghRename[t.id] ?? '';
                        const replaceVal = ghReplaceToken[t.id] ?? '';
                        return (
                          <li
                            key={t.id}
                            className="rounded-md border border-border/60 p-4 space-y-3"
                          >
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium text-foreground">{t.label}</span>
                              {t.hasToken ? (
                                <span className="text-xs text-muted-foreground">···{t.tokenSuffix}</span>
                              ) : (
                                <span className="text-xs text-amber-600 dark:text-amber-400">No secret stored</span>
                              )}
                              {isActive && (
                                <span className="inline-flex items-center gap-1 rounded-full bg-primary/15 text-primary px-2 py-0.5 text-xs font-medium">
                                  <Star className="h-3 w-3" aria-hidden />
                                  Active
                                </span>
                              )}
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                disabled={!!ghBusy || isActive || !t.hasToken}
                                onClick={() => void runGithubTokenAction({ action: 'setActive', id: t.id }, `act-${t.id}`)}
                              >
                                Use as active
                              </Button>
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                className="text-destructive hover:text-destructive"
                                disabled={!!ghBusy}
                                onClick={() => {
                                  if (!confirm(`Remove GitHub token “${t.label}”?`)) return;
                                  void runGithubTokenAction({ action: 'delete', id: t.id }, `del-${t.id}`).then(() => {
                                    setGhRename((prev) => {
                                      const next = { ...prev };
                                      delete next[t.id];
                                      return next;
                                    });
                                    setGhReplaceToken((prev) => {
                                      const next = { ...prev };
                                      delete next[t.id];
                                      return next;
                                    });
                                  });
                                }}
                              >
                                <Trash2 className="h-4 w-4 mr-1" />
                                Remove
                              </Button>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
                              <div className="space-y-1">
                                <Label className="text-xs">Rename</Label>
                                <Input
                                  placeholder={t.label}
                                  value={renameVal}
                                  onChange={(e) => setGhRename((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  autoComplete="off"
                                />
                              </div>
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                disabled={!!ghBusy || !renameVal.trim()}
                                onClick={() => {
                                  const label = renameVal.trim();
                                  if (!label) return;
                                  void runGithubTokenAction({ action: 'upsert', id: t.id, label }, `ren-${t.id}`).then(() => {
                                    setGhRename((prev) => ({ ...prev, [t.id]: '' }));
                                  });
                                }}
                              >
                                Save label
                              </Button>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
                              <div className="space-y-1">
                                <Label className="text-xs">Replace PAT</Label>
                                <Input
                                  type="password"
                                  placeholder="New token"
                                  value={replaceVal}
                                  onChange={(e) => setGhReplaceToken((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  autoComplete="new-password"
                                />
                              </div>
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                disabled={!!ghBusy || !replaceVal.trim()}
                                onClick={() => {
                                  const token = replaceVal.trim();
                                  if (!token) return;
                                  void runGithubTokenAction({ action: 'upsert', id: t.id, token }, `tok-${t.id}`).then(() => {
                                    setGhReplaceToken((prev) => ({ ...prev, [t.id]: '' }));
                                  });
                                }}
                              >
                                Update PAT
                              </Button>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>

                <div className="space-y-4 pt-2 border-t border-border/40">
                  <div>
                    <Label>Azure DevOps</Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Each entry keeps a <strong>PAT and org URL together</strong>. The <strong>active</strong> pair is written to{' '}
                      <code className="text-[10px]">ADO_TOKEN</code> and <code className="text-[10px]">ADO_ORG_URL</code>.
                    </p>
                  </div>

                  <div className="rounded-md border border-border/60 bg-muted/20 p-4 space-y-3">
                    <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Add credential</div>
                    <div className="grid gap-2">
                      <Input
                        placeholder="Label (e.g. Contoso prod)"
                        value={newAdoLabel}
                        onChange={(e) => setNewAdoLabel(e.target.value)}
                        autoComplete="off"
                      />
                      <Input
                        placeholder="https://dev.azure.com/yourorg"
                        value={newAdoOrgUrl}
                        onChange={(e) => setNewAdoOrgUrl(e.target.value)}
                        autoComplete="off"
                      />
                      <Input
                        type="password"
                        placeholder="ADO PAT"
                        value={newAdoToken}
                        onChange={(e) => setNewAdoToken(e.target.value)}
                        autoComplete="new-password"
                      />
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      disabled={!!adoBusy}
                      onClick={() => {
                        const label = newAdoLabel.trim();
                        const orgUrl = newAdoOrgUrl.trim();
                        const token = newAdoToken.trim();
                        if (!label || !orgUrl || !token) {
                          setRepoSaveError('Label, organization URL, and PAT are required to add an ADO credential.');
                          return;
                        }
                        void runAdoTokenAction({ action: 'upsert', label, orgUrl, token }, 'ado-add').then((ok) => {
                          if (ok) {
                            setNewAdoLabel('');
                            setNewAdoOrgUrl('');
                            setNewAdoToken('');
                          }
                        });
                      }}
                    >
                      <Plus className="h-4 w-4 mr-1" />
                      Add credential
                    </Button>
                  </div>

                  {adoCredentials.length > 0 && (
                    <ul className="space-y-4">
                      {adoCredentials.map((t) => {
                        const isActive = t.id === activeAdoId;
                        const renameVal = adoRename[t.id] ?? '';
                        const replaceTok = adoReplaceToken[t.id] ?? '';
                        const replaceOrg = adoReplaceOrg[t.id] ?? '';
                        return (
                          <li
                            key={t.id}
                            className="rounded-md border border-border/60 p-4 space-y-3"
                          >
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium text-foreground">{t.label}</span>
                              {t.orgUrl ? (
                                <span className="text-xs text-muted-foreground truncate max-w-[min(100%,280px)]" title={t.orgUrl}>
                                  {t.orgUrl}
                                </span>
                              ) : (
                                <span className="text-xs text-muted-foreground">No org URL</span>
                              )}
                              {t.hasToken ? (
                                <span className="text-xs text-muted-foreground">···{t.tokenSuffix}</span>
                              ) : (
                                <span className="text-xs text-amber-600 dark:text-amber-400">No PAT stored</span>
                              )}
                              {isActive && (
                                <span className="inline-flex items-center gap-1 rounded-full bg-primary/15 text-primary px-2 py-0.5 text-xs font-medium">
                                  <Star className="h-3 w-3" aria-hidden />
                                  Active
                                </span>
                              )}
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                disabled={!!adoBusy || isActive || !t.hasToken}
                                onClick={() => void runAdoTokenAction({ action: 'setActive', id: t.id }, `ado-act-${t.id}`)}
                              >
                                Use as active
                              </Button>
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                className="text-destructive hover:text-destructive"
                                disabled={!!adoBusy}
                                onClick={() => {
                                  if (!confirm(`Remove ADO credential “${t.label}”?`)) return;
                                  void runAdoTokenAction({ action: 'delete', id: t.id }, `ado-del-${t.id}`).then(() => {
                                    setAdoRename((prev) => {
                                      const n = { ...prev };
                                      delete n[t.id];
                                      return n;
                                    });
                                    setAdoReplaceToken((prev) => {
                                      const n = { ...prev };
                                      delete n[t.id];
                                      return n;
                                    });
                                    setAdoReplaceOrg((prev) => {
                                      const n = { ...prev };
                                      delete n[t.id];
                                      return n;
                                    });
                                  });
                                }}
                              >
                                <Trash2 className="h-4 w-4 mr-1" />
                                Remove
                              </Button>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
                              <div className="space-y-1">
                                <Label className="text-xs">Rename</Label>
                                <Input
                                  placeholder={t.label}
                                  value={renameVal}
                                  onChange={(e) => setAdoRename((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  autoComplete="off"
                                />
                              </div>
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                disabled={!!adoBusy || !renameVal.trim()}
                                onClick={() => {
                                  const label = renameVal.trim();
                                  if (!label) return;
                                  void runAdoTokenAction({ action: 'upsert', id: t.id, label }, `ado-ren-${t.id}`).then((ok) => {
                                    if (ok) setAdoRename((prev) => ({ ...prev, [t.id]: '' }));
                                  });
                                }}
                              >
                                Save label
                              </Button>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
                              <div className="space-y-1">
                                <Label className="text-xs">Replace org URL</Label>
                                <Input
                                  placeholder={t.orgUrl || 'https://dev.azure.com/…'}
                                  value={replaceOrg}
                                  onChange={(e) => setAdoReplaceOrg((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  autoComplete="off"
                                />
                              </div>
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                disabled={!!adoBusy || !replaceOrg.trim()}
                                onClick={() => {
                                  const orgUrl = replaceOrg.trim();
                                  if (!orgUrl) return;
                                  void runAdoTokenAction({ action: 'upsert', id: t.id, orgUrl }, `ado-org-${t.id}`).then((ok) => {
                                    if (ok) setAdoReplaceOrg((prev) => ({ ...prev, [t.id]: '' }));
                                  });
                                }}
                              >
                                Update URL
                              </Button>
                            </div>
                            <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
                              <div className="space-y-1">
                                <Label className="text-xs">Replace PAT</Label>
                                <Input
                                  type="password"
                                  placeholder="New PAT"
                                  value={replaceTok}
                                  onChange={(e) => setAdoReplaceToken((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  autoComplete="new-password"
                                />
                              </div>
                              <Button
                                type="button"
                                variant="secondary"
                                size="sm"
                                disabled={!!adoBusy || !replaceTok.trim()}
                                onClick={() => {
                                  const token = replaceTok.trim();
                                  if (!token) return;
                                  void runAdoTokenAction({ action: 'upsert', id: t.id, token }, `ado-tok-${t.id}`).then((ok) => {
                                    if (ok) setAdoReplaceToken((prev) => ({ ...prev, [t.id]: '' }));
                                  });
                                }}
                              >
                                Update PAT
                              </Button>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-3 pt-4">
                  {repoSaveError && <span className="text-sm text-destructive">{repoSaveError}</span>}
                  {repoSaveSuccess && (
                     <span className="text-sm text-green-600">Saved. Applied dynamically to all agents.</span>
                  )}
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>

          <AccordionItem value="system-infrastructure">
            <AccordionTrigger>
              <div className="flex items-center justify-between w-full">
                <span>System Infrastructure</span>
                <span className="text-xs text-muted-foreground">
                  ELK / Vault
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-6 pt-4">
                {sysIsLoading && (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Loading system settings…
                  </div>
                )}
                {sysError && (
                  <p className="text-sm text-destructive">{String(sysError)}</p>
                )}
                
                <div className="space-y-4">
                  <div>
                    <Label>Elasticsearch Configuration</Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Provide external telemetry URLs when running dynamically outside the standard Docker topology.
                    </p>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div className="space-y-2">
                      <Label className="text-xs">Elasticsearch URL</Label>
                      <Input
                        placeholder="http://127.0.0.1:9200"
                        value={sysForm.es_url ?? effectiveSys.es_url ?? ''}
                        onChange={(e) => setSysForm(p => ({ ...p, es_url: e.target.value }))}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">ES API Key</Label>
                      <Input
                        type="password"
                        placeholder="Elasticsearch Key"
                        value={sysForm.es_api_key ?? (effectiveSys.es_api_key === '***' ? '' : (effectiveSys.es_api_key ?? ''))}
                        onChange={(e) => setSysForm(p => ({ ...p, es_api_key: e.target.value }))}
                      />
                    </div>
                  </div>
                </div>

                <div className="space-y-4 pt-4 border-t border-border/40">
                  <div>
                    <Label>OpenBao Vault Configuration</Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Point Flume to an existing Vault instance.
                    </p>
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    <div className="space-y-2">
                      <Label className="text-xs">Vault URL</Label>
                      <Input
                        placeholder="http://127.0.0.1:8200"
                        value={sysForm.openbao_url ?? effectiveSys.openbao_url ?? ''}
                        onChange={(e) => setSysForm(p => ({ ...p, openbao_url: e.target.value }))}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label className="text-xs">Root Token</Label>
                      <Input
                        type="password"
                        placeholder="Vault Root Token"
                        value={sysForm.vault_token ?? (effectiveSys.vault_token === '••••' ? '' : (effectiveSys.vault_token ?? ''))}
                        onChange={(e) => setSysForm(p => ({ ...p, vault_token: e.target.value }))}
                      />
                    </div>
                  </div>
                </div>

                <div className="space-y-4 pt-4 border-t border-border/40">
                  <div>
                    <Label>Observability</Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Toggle system telemetry routing to the Prometheus /metrics endpoint.
                    </p>
                  </div>
                  <div className="flex items-center space-x-2">
                    <Switch
                      checked={sysForm.prometheus_enabled ?? effectiveSys.prometheus_enabled ?? true}
                      onCheckedChange={(v) => setSysForm(p => ({ ...p, prometheus_enabled: v }))}
                    />
                    <Label>Enable Prometheus Telemetry</Label>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-3 pt-4 border-t border-border/40">
                  <Button 
                    onClick={() => {
                      sysMutation.mutate({
                        es_url: effectiveSys.es_url ?? 'http://127.0.0.1:9200',
                        es_api_key: sysForm.es_api_key ?? (effectiveSys.es_api_key ?? ''),
                        openbao_url: effectiveSys.openbao_url ?? 'http://127.0.0.1:8200',
                        vault_token: sysForm.vault_token ?? (effectiveSys.vault_token ?? ''),
                        prometheus_enabled: sysForm.prometheus_enabled ?? effectiveSys.prometheus_enabled ?? true,
                      });
                    }} 
                    disabled={sysMutation.isPending}
                  >
                    {sysMutation.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Save className="h-4 w-4" />
                    )}
                    Save System Config
                  </Button>
                  {sysSaveError && <span className="text-sm text-destructive">{sysSaveError}</span>}
                  {sysSaveSuccess && <span className="text-sm text-green-600">System config saved.</span>}
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}
