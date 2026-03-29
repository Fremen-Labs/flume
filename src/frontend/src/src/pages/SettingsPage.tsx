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

async function saveLlmSettings(payload: LlmSettingsPayload): Promise<{ ok: boolean; restartRequired: boolean }> {
  const res = await fetch('/api/settings/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await parseJsonBody<{ ok?: boolean; restartRequired?: boolean; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Save failed: ${res.status}`);
  return data as { ok: boolean; restartRequired: boolean };
}

async function llmCredentialAction(payload: LlmCredentialActionPayload): Promise<{ ok: boolean; restartRequired?: boolean }> {
  const res = await fetch('/api/settings/llm/credentials', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await parseJsonBody<{ ok?: boolean; restartRequired?: boolean; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Request failed: ${res.status}`);
  return data as { ok: boolean; restartRequired?: boolean };
}

async function fetchCodexAppServerStatus(): Promise<CodexAppServerStatusResponse> {
  const res = await fetch('/api/codex-app-server/status');
  if (!res.ok) throw new Error(`Codex app-server status failed: ${res.status}`);
  return res.json();
}

async function refreshOAuth(): Promise<{ ok: boolean; message?: string; restartRequired?: boolean }> {
  const res = await fetch('/api/settings/llm/oauth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const data = await parseJsonBody<{ ok?: boolean; message?: string; restartRequired?: boolean; error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Refresh failed: ${res.status}`);
  return data as { ok: boolean; message?: string; restartRequired?: boolean };
}

async function fetchRepoSettings(): Promise<RepoSettingsResponse> {
  const res = await fetch('/api/settings/repos');
  const data = await parseJsonBody<RepoSettingsResponse & { error?: string }>(res);
  if (!res.ok) throw new Error(data?.error || `Repo settings fetch failed: ${res.status}`);
  return data;
}

async function restartFlumeServices(): Promise<{
  ok: boolean;
  mode?: string;
  message?: string;
  error?: string;
}> {
  const res = await fetch('/api/settings/restart-services', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const data = await parseJsonBody<{ ok?: boolean; mode?: string; message?: string; error?: string }>(res);
  if (!res.ok) {
    throw new Error(
      data?.error ||
        (res.status === 404
          ? 'Restart API not found — restart the dashboard so server.py includes POST /api/settings/restart-services.'
          : `Restart failed: ${res.status}`),
    );
  }
  return data as { ok: boolean; mode?: string; message?: string; error?: string };
}

/** Survives leaving Settings and coming back (SPA remount). Cleared after restart succeeds. */
const FLUME_PENDING_RESTART_KEY = 'flume-pending-service-restart';

function readPendingRestartFlag(): boolean {
  try {
    return sessionStorage.getItem(FLUME_PENDING_RESTART_KEY) === '1';
  } catch {
    return false;
  }
}

function setPendingRestartFlag(on: boolean) {
  try {
    if (on) sessionStorage.setItem(FLUME_PENDING_RESTART_KEY, '1');
    else sessionStorage.removeItem(FLUME_PENDING_RESTART_KEY);
  } catch {
    /* private / quota */
  }
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
  const [showRestartCta, setShowRestartCta] = useState(readPendingRestartFlag);
  const [restartInfo, setRestartInfo] = useState<string | null>(null);
  const [restartError, setRestartError] = useState<string | null>(null);

  const saveMutation = useMutation({
    mutationFn: saveLlmSettings,
    onSuccess: (data) => {
      setSaveError(null);
      setSaveSuccess(true);
      queryClient.invalidateQueries({ queryKey: ['settings', 'llm'] });
      setTimeout(() => setSaveSuccess(false), 3000);
      if (data.restartRequired !== false) {
        setPendingRestartFlag(true);
        setShowRestartCta(true);
      }
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
      if (data.restartRequired !== false) {
        setPendingRestartFlag(true);
        setShowRestartCta(true);
      }
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
      setPendingRestartFlag(true);
      setShowRestartCta(true);
    },
    onError: (e: Error) => {
      setSysSaveError(e.message);
    },
  });

  const effectiveSys = { ...sysData, ...sysForm };

  const restartServicesMutation = useMutation({
    mutationFn: restartFlumeServices,
    onSuccess: (data) => {
      setRestartError(null);
      setPendingRestartFlag(false);
      setShowRestartCta(false);
      setRestartInfo(data.message ?? 'Restart initiated.');
      setTimeout(() => setRestartInfo(null), 12_000);
    },
    onError: (e: unknown) => {
      const msg = e instanceof Error ? e.message : String(e);
      if (
        e instanceof TypeError ||
        msg === 'Failed to fetch' ||
        msg.includes('NetworkError') ||
        msg.includes('Load failed')
      ) {
        setPendingRestartFlag(false);
        setShowRestartCta(false);
        setRestartError(null);
        setRestartInfo('Connection closed — restart is probably running. Wait a few seconds and refresh the page.');
        setTimeout(() => setRestartInfo(null), 12_000);
        return;
      }
      setRestartError(msg);
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
      if (credRes.restartRequired) {
        setPendingRestartFlag(true);
        setShowRestartCta(true);
      }
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

      {showRestartCta && (
        <div
          className="rounded-xl border-2 border-amber-500/50 bg-amber-500/15 dark:bg-amber-950/40 px-4 py-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between shadow-sm"
          data-testid="settings-restart-services-cta"
        >
          <div className="space-y-1 min-w-0">
            <p className="text-sm font-semibold text-foreground">Apply saved settings</p>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Restart the dashboard and worker manager so workers and background tasks pick up LLM, credentials, and repo
              tokens.
            </p>
          </div>
          <Button
            type="button"
            variant="default"
            className="shrink-0 font-semibold"
            disabled={restartServicesMutation.isPending}
            onClick={() => {
              setRestartError(null);
              setRestartInfo(null);
              restartServicesMutation.mutate();
            }}
          >
            {restartServicesMutation.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <RotateCcw className="h-4 w-4" />
            )}
            <span className="ml-2">Restart services</span>
          </Button>
        </div>
      )}

      {restartError && (
        <p className="text-sm text-destructive flex items-center gap-2">
          <AlertCircle className="h-4 w-4 shrink-0" />
          {restartError}
        </p>
      )}
      {restartInfo && (
        <p className="text-sm text-emerald-700 dark:text-emerald-400 flex items-center gap-2">
          <RotateCcw className="h-4 w-4 shrink-0" />
          {restartInfo}
        </p>
      )}

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
                {exoData?.active && (userPerspective === 'exo_administrator' || userPerspective === 'developer') && (
                  <div className="rounded-xl border-2 border-emerald-500/50 bg-emerald-500/15 dark:bg-emerald-950/40 px-4 py-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between shadow-sm">
                    <div className="space-y-1 min-w-0">
                      <p className="text-sm font-semibold text-emerald-800 dark:text-emerald-300">Exo Cluster Detected Locally!</p>
                      <p className="text-xs text-emerald-700 dark:text-emerald-400/80 leading-relaxed">
                        A fast Apple Silicon cluster daemon is running on your host machine.
                      </p>
                    </div>
                    <Button
                      type="button"
                      className="shrink-0 font-semibold bg-emerald-600 hover:bg-emerald-700 text-white"
                      title={exoData?.baseUrl ? `Set endpoint to ${exoData.baseUrl}` : "Configure Exo"}
                      onClick={() => {
                        if (exoData?.active && exoData.baseUrl) {
                          updateForm({
                            provider: 'openai_compatible',
                            baseUrl: exoData.baseUrl,
                            authMode: 'api_key',
                            apiKey: '',
                            model: ''
                          });
                        }
                      }}
                    >
                      <Plus className="h-4 w-4 mr-1" />
                      Configure Exo
                    </Button>
                  </div>
                )}
                <div className="space-y-2">
                  <Label>Provider</Label>
                  <Select
                    value={providerId}
                    onValueChange={(v) => {
                      const p = catalog.find((x: LlmSettingsCatalogItem) => x.id === v);
                      // Default to API key whenever provider changes to avoid OAuth getting "stuck".
                      // Clear key/credential so another vendor's masked key is not reused.
                      updateForm({
                        provider: v,
                        authMode: 'api_key',
                        credentialId: '',
                        credentialLabel: '',
                        apiKey: '',
                        ...(p?.models?.length ? { model: p.models[0].id } : {}),
                      });
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
                      Only keys for the <strong>provider you picked above</strong> appear here (not keys for other
                      vendors). Each label must be unique per provider. Keys live in{' '}
                      <code className="text-[10px]">worker-manager/llm_credentials.json</code>.{' '}
                      <strong>Set as default</strong> applies that key to the global LLM profile (LLM_*) and is the
                      fallback for agent roles that use &quot;Settings (default)&quot;.
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
                            {defaultCredId === c.id && (
                              <span className="text-[10px] font-medium uppercase tracking-wide text-emerald-600 dark:text-emerald-400">
                                Default
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
                                void runCredAction(
                                  { action: 'activate', id: c.id },
                                  'Default key updated — LLM profile and agent fallback use this key.',
                                )
                              }
                            >
                              Set as default
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
                        Unique among all <strong>{providerName}</strong> keys. Save stores the key for this provider; use{' '}
                        <strong>Set as default</strong> on a saved row if you want it to drive the global LLM profile and
                        agent &quot;Settings (default)&quot; fallback.
                      </p>
                    </div>
                    <div className="space-y-2">
                      <Label>API Key</Label>
                      {showPersistedMaskedApiKey && (
                        <p className="text-xs text-emerald-600 dark:text-emerald-400">
                          Key is saved
                          {data?.settings?.keySuffix
                            ? ` (ends with ···${data.settings.keySuffix})`
                            : ''}
                          . Paste a new key only if you want to replace it.
                        </p>
                      )}
                      <Input
                        type="password"
                        placeholder={
                          showPersistedMaskedApiKey ? 'Leave blank to keep saved key' : 'sk-… or paste key'
                        }
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
                  <div className="space-y-4 p-4 rounded-lg bg-muted/50 border border-amber-500/25">
                    <p className="text-xs text-amber-800 dark:text-amber-200/95 leading-relaxed font-medium">
                      <strong>Plan New Work</strong> now auto-routes through the <strong>Codex CLI</strong>{' '}
                      (<code className="text-[11px]">codex app-server</code> on stdio when OAuth + Codex auth are present) so you can use{' '}
                      <strong>ChatGPT/Codex subscription OAuth</strong> without a platform <code className="text-[11px]">sk-</code>{' '}
                      key. Install Node, <code className="text-[11px]">npm i -g @openai/codex</code> or <code className="text-[11px]">npx</code>, run{' '}
                      <code className="text-[11px]">codex login</code> so <code className="text-[11px]">~/.codex/auth.json</code>{' '}
                      exists (Flume&apos;s <code className="text-[11px]">.openai-oauth.json</code> is not read by Codex).
                    </p>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                      <strong>Worker agents</strong> that use OpenAI-style <strong>tool calling</strong> still hit{' '}
                      <code className="text-[11px]">api.openai.com</code> today — use a platform <code className="text-[11px]">sk-</code>,{' '}
                      switch Auth to <strong>API Key</strong>, or use <strong>Ollama</strong> for those roles until full Codex
                      bridge support. <code className="text-[11px]">./flume codex-oauth</code> syncs tokens into Flume for UI refresh;
                      subscription limits still apply per OpenAI/Codex.
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
                        {data.oauthStatus.accessTokenJwtParsed &&
                          data.oauthStatus.hasModelRequestScope === false && (
                            <p className="text-amber-700 dark:text-amber-400">
                              JWT has no <code className="text-[11px]">model.request</code> —{' '}
                              <code className="text-[11px]">/v1/chat/completions</code> (Plan New Work) will return 401
                              unless you use a platform <code className="text-[11px]">sk-</code> key.
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
                    <span className="text-sm text-green-600">Saved.</span>
                  )}
                </div>

                {data?.openbaoInstalled === false && (
                  <p className="text-xs text-destructive">
                    OpenBao is not installed. Sensitive settings will be stored in an insecure local <code>.env</code> file.
                  </p>
                )}
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

                <div className="flex flex-wrap items-center gap-3 pt-4">
                  <Button 
                    onClick={() => {
                      sysMutation.mutate({
                        es_url: effectiveSys.es_url ?? 'http://127.0.0.1:9200',
                        es_api_key: sysForm.es_api_key ?? (effectiveSys.es_api_key ?? ''),
                        openbao_url: effectiveSys.openbao_url ?? 'http://127.0.0.1:8200',
                        vault_token: sysForm.vault_token ?? (effectiveSys.vault_token ?? '')
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
                  {sysSaveSuccess && <span className="text-sm text-green-600">Saved System Config. Restart dashboard and workers to apply.</span>}
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}
