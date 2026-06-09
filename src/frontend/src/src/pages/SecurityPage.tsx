import React, { useEffect, useState } from 'react';
import { Shield, Key, Lock, Unlock, Eye, EyeOff, Activity, AlertCircle, Copy, Check, Plus, Trash2, Edit2, X, ChevronDown } from 'lucide-react';
import { GlassMetricCard } from '@/components/GlassMetricCard';

import { createLogger } from '@/utils/logger';
const logger = createLogger('pages.SecurityPage');

interface SecurityData {
  vault_active: boolean;
  openbao_keys: Record<string, string>;
  audit_logs: Array<{
    '@timestamp': string;
    message: string;
    agent_roles: string;
    worker_name: string;
    secret_path: string;
    keys_retrieved: string[];
  }>;
}

// ── Credential Type Definitions ─────────────────────────────────────────
type CredentialType = 'llm' | 'repo';

interface ProviderOption {
  id: string;
  label: string;
  keyName: string;
  valuePlaceholder: string;
}

const LLM_PROVIDERS: ProviderOption[] = [
  { id: 'openai', label: 'OpenAI', keyName: 'OPENAI_API_KEY', valuePlaceholder: 'sk-...' },
  { id: 'anthropic', label: 'Anthropic', keyName: 'ANTHROPIC_API_KEY', valuePlaceholder: 'sk-ant-...' },
  { id: 'gemini', label: 'Google Gemini', keyName: 'GEMINI_API_KEY', valuePlaceholder: 'AIza...' },
  { id: 'xai', label: 'xAI (Grok)', keyName: 'XAI_API_KEY', valuePlaceholder: 'xai-...' },
];

const REPO_PROVIDERS: ProviderOption[] = [
  { id: 'github', label: 'GitHub', keyName: 'GH_TOKEN', valuePlaceholder: 'ghp_...' },
  { id: 'ado', label: 'Azure DevOps', keyName: 'ADO_TOKEN', valuePlaceholder: 'Paste your ADO Personal Access Token...' },
];

export default function SecurityPage() {
  const [data, setData] = useState<SecurityData | null>(null);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Authorization token
  const [adminToken, setAdminToken] = useState(() => localStorage.getItem('flume-admin-token') || '');
  const [tokenValid, setTokenValid] = useState<boolean | null>(null);
  const [validatingToken, setValidatingToken] = useState(false);

  // Secret operation states
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, string>>({});
  const [copiedSecret, setCopiedSecret] = useState<string | null>(null);

  // Modal states
  const [showAddModal, setShowAddModal] = useState(false);
  const [editSecretKey, setEditSecretKey] = useState<string | null>(null);
  const [modalKey, setModalKey] = useState('');
  const [modalValue, setModalValue] = useState('');
  const [modalError, setModalError] = useState<string | null>(null);
  const [modalSubmitting, setModalSubmitting] = useState(false);

  // New credential type states for the Add modal
  const [credentialType, setCredentialType] = useState<CredentialType | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string>('');

  const handleTokenChange = (val: string) => {
    setAdminToken(val);
    localStorage.setItem('flume-admin-token', val);
    setTokenValid(null); // require explicit re-validation when token changes
  };

  const validateAdminToken = async () => {
    if (!adminToken) {
      setTokenValid(false);
      return;
    }
    setValidatingToken(true);
    try {
      const res = await fetch('/api/security/validate', {
        method: 'GET',
        headers: getHeaders(),
      });
      setTokenValid(res.ok);
    } catch {
      setTokenValid(false);
    } finally {
      setValidatingToken(false);
    }
  };

  const getHeaders = () => {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (adminToken) {
      headers['Authorization'] = `Bearer ${adminToken}`;
    }
    return headers;
  };

  const fetchSecurityData = () => {
    fetch('/api/security', {
      headers: getHeaders(),
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        logger.error('fetchSecurityData', 'Security data fetch failed', { error: String(err) });
        setError(err.message);
        setLoading(false);
      });
  };

  useEffect(() => {
    fetchSecurityData();
  }, [adminToken]);

  const handleReveal = async (key: string) => {
    if (revealedSecrets[key]) {
      // Toggle visibility off (remove cached value)
      setRevealedSecrets((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      return;
    }

    try {
      const res = await fetch('/api/security/secrets/reveal', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `HTTP ${res.status}`);
      }
      const json = await res.json();
      setRevealedSecrets((prev) => ({ ...prev, [key]: json.value }));
    } catch (err: any) {
      alert(`Failed to reveal secret: ${err.message}`);
    }
  };

  const handleCopy = async (key: string) => {
    try {
      let value = revealedSecrets[key];
      if (!value) {
        const res = await fetch('/api/security/secrets/reveal', {
          method: 'POST',
          headers: getHeaders(),
          body: JSON.stringify({ key }),
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.error || `HTTP ${res.status}`);
        }
        const json = await res.json();
        value = json.value;
      }
      await navigator.clipboard.writeText(value);
      setCopiedSecret(key);
      setTimeout(() => setCopiedSecret(null), 2000);
    } catch (err: any) {
      alert(`Failed to copy secret: ${err.message}`);
    }
  };

  // Resolve the correct vault key name from the selected credential type + provider
  const resolveKeyName = (): string => {
    if (!credentialType || !selectedProvider) return '';
    const providers = credentialType === 'llm' ? LLM_PROVIDERS : REPO_PROVIDERS;
    const match = providers.find(p => p.id === selectedProvider);
    return match?.keyName || '';
  };

  // Get value placeholder for the selected provider
  const getValuePlaceholder = (): string => {
    if (!credentialType || !selectedProvider) return 'Enter the secure token/credentials...';
    const providers = credentialType === 'llm' ? LLM_PROVIDERS : REPO_PROVIDERS;
    const match = providers.find(p => p.id === selectedProvider);
    return match?.valuePlaceholder || 'Enter the secure token/credentials...';
  };

  // Get value label for the selected credential type
  const getValueLabel = (): string => {
    if (credentialType === 'llm') return 'API Key';
    if (credentialType === 'repo') return 'Access Token';
    return 'Secret Plaintext Value';
  };

  const handleSaveSecret = async (e: React.FormEvent) => {
    e.preventDefault();

    // For "Add" mode, resolve the key from the credential type + provider
    const effectiveKey = showAddModal ? resolveKeyName() : modalKey;

    if (!effectiveKey || !modalValue) {
      setModalError(showAddModal ? 'Please select a provider and enter a value' : 'Key and value are required');
      return;
    }
    setModalSubmitting(true);
    setModalError(null);
    try {
      const res = await fetch('/api/security/secrets/update', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ key: effectiveKey, value: modalValue }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `HTTP ${res.status}`);
      }
      fetchSecurityData();
      closeModal();
    } catch (err: any) {
      setModalError(err.message);
    } finally {
      setModalSubmitting(false);
    }
  };

  const closeModal = () => {
    setShowAddModal(false);
    setEditSecretKey(null);
    setModalKey('');
    setModalValue('');
    setModalError(null);
    setCredentialType(null);
    setSelectedProvider('');
  };

  const handleDeleteSecret = async (key: string) => {
    if (!confirm(`Are you sure you want to delete the secret key "${key}"?`)) {
      return;
    }
    try {
      const res = await fetch('/api/security/secrets/delete', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `HTTP ${res.status}`);
      }
      fetchSecurityData();
      // Remove from revealed secrets if deleted
      setRevealedSecrets((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    } catch (err: any) {
      alert(`Failed to delete secret: ${err.message}`);
    }
  };

  if (loading) {
    return (
      <div className="p-8 flex items-center justify-center min-h-[50vh]">
        <div className="flex flex-col items-center text-muted-foreground gap-4">
          <Shield className="w-12 h-12 text-blue-500/50 animate-pulse" />
          <p className="text-sm font-medium">Establishing Secure Neural Link...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <div className="p-6 border border-destructive/20 bg-destructive/5 rounded-xl text-destructive flex gap-4">
          <AlertCircle className="w-6 h-6 flex-shrink-0" />
          <div>
            <h3 className="font-semibold mb-1">Security Systems Offline</h3>
            <p className="text-sm opacity-80">{error}</p>
          </div>
        </div>
      </div>
    );
  }

  const { vault_active, openbao_keys, audit_logs } = data || {};
  const rootKeys = Object.keys(openbao_keys || {});
  const computedKeyName = resolveKeyName();

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8 animate-fade-in pb-24">
      {/* Header & Token Input */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-border/40 pb-6">
        <div className="flex flex-col gap-2">
          <h1 className="text-3xl font-bold tracking-tight text-foreground flex items-center gap-3">
            <Shield className="w-8 h-8 text-primary" />
            OpenBao Hive Security
          </h1>
          <p className="text-muted-foreground text-sm max-w-2xl">
            Real-time auditing of Flume vault retrievals and cryptographic bindings natively processed via Elasticsearch telemetry tracing.
          </p>
        </div>

        {/* Admin Token Field */}
        <div className="flex flex-col gap-1.5 w-full md:w-auto md:min-w-[320px]">
          <div className="text-[10px] font-semibold tracking-[0.5px] text-muted-foreground/80 pl-1">FLume Admin Token</div>
          <div className="flex items-center gap-2 bg-card/45 border border-border/50 rounded-xl p-2.5 backdrop-blur-md shadow-sm">
            <button
              type="button"
              onClick={validateAdminToken}
              disabled={validatingToken || !adminToken}
              title={tokenValid === true ? "Token is valid" : tokenValid === false ? "Token is invalid" : "Click to validate token"}
              className={`p-1 rounded-md flex-shrink-0 transition-all active:scale-95 disabled:opacity-50 ${validatingToken ? 'animate-pulse' : ''
                } ${tokenValid === true
                  ? 'text-emerald-500 hover:bg-emerald-500/10'
                  : tokenValid === false
                    ? 'text-destructive hover:bg-destructive/10'
                    : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'
                }`}
            >
              {tokenValid === true ? <Unlock className="w-4 h-4" /> : <Lock className="w-4 h-4" />}
            </button>
            <input
              type="password"
              placeholder="Enter Admin Token..."
              value={adminToken}
              onChange={(e) => handleTokenChange(e.target.value)}
              className="bg-transparent border-0 outline-none text-xs w-full text-foreground placeholder:text-muted-foreground/60"
            />
            {tokenValid === true && <Check className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" />}
            {tokenValid === false && adminToken && <AlertCircle className="w-3.5 h-3.5 text-destructive flex-shrink-0" />}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <GlassMetricCard
          title="Vault Status"
          value={vault_active ? "SECURED" : "DETACHED"}
          icon={Lock}
          trend={vault_active ? { value: 100, label: 'connected' } : undefined}
          subtitle="OpenBao Agent Connection"
        />
        <GlassMetricCard
          title="Unique Vault Secrets"
          value={rootKeys.length}
          icon={Key}
          subtitle="Vaulted Identity Properties"
        />
        <GlassMetricCard
          title="Checkout Events"
          value={audit_logs?.length || 0}
          icon={Activity}
          subtitle="Checkout Events Tracked"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Vault Keys */}
        <div className="lg:col-span-1 border border-border bg-card/50 rounded-xl p-6 shadow-sm overflow-hidden flex flex-col gap-4">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <Key className="w-5 h-5 text-primary" />
              <h2 className="text-lg font-bold">Secure Vault KV Store</h2>
            </div>
            {vault_active && (
              <button
                onClick={() => {
                  setModalKey('');
                  setModalValue('');
                  setModalError(null);
                  setCredentialType(null);
                  setSelectedProvider('');
                  setShowAddModal(true);
                }}
                className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg bg-primary text-primary-foreground text-xs font-semibold hover:bg-primary/95 transition-all shadow-sm"
              >
                <Plus className="w-3.5 h-3.5" />
                Add Secret
              </button>
            )}
          </div>
          {rootKeys.length === 0 ? (
            <div className="text-muted-foreground text-sm italic">No keys actively stored in `secret/flume`.</div>
          ) : (
            <div className="flex flex-col gap-3 max-h-[500px] overflow-y-auto pr-1">
              {rootKeys.map((key) => {
                const isRevealed = !!revealedSecrets[key];
                const isCopied = copiedSecret === key;
                return (
                  <div key={key} className="p-3 border border-border/50 rounded-lg flex flex-col gap-2 bg-background/30 backdrop-blur-sm group hover:border-primary/30 transition-all">
                    <div className="flex items-center justify-between">
                      <div className="font-mono text-sm font-semibold truncate max-w-[160px]" title={key}>{key}</div>

                      {/* Action buttons */}
                      <div className="flex items-center gap-1.5 opacity-60 group-hover:opacity-100 transition-opacity">
                        <button
                          onClick={() => handleReveal(key)}
                          title={isRevealed ? "Hide Secret" : "Reveal Secret"}
                          className="p-1 rounded hover:bg-muted/80 text-muted-foreground hover:text-foreground transition-colors"
                        >
                          {isRevealed ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                        </button>
                        <button
                          onClick={() => handleCopy(key)}
                          title="Copy Plaintext"
                          className={`p-1 rounded hover:bg-muted/80 transition-colors ${isCopied ? 'text-green-500' : 'text-muted-foreground hover:text-foreground'}`}
                        >
                          {isCopied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                        </button>
                        <button
                          onClick={() => {
                            setModalKey(key);
                            setModalValue(revealedSecrets[key] || '');
                            setModalError(null);
                            setEditSecretKey(key);
                          }}
                          title="Edit Secret"
                          className="p-1 rounded hover:bg-muted/80 text-muted-foreground hover:text-foreground transition-colors"
                        >
                          <Edit2 className="w-3.5 h-3.5" />
                        </button>
                        <button
                          onClick={() => handleDeleteSecret(key)}
                          title="Delete Secret"
                          className="p-1 rounded hover:bg-muted/80 text-muted-foreground hover:text-destructive transition-colors"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    {/* Plaintext / Mask Display */}
                    <div className="font-mono text-xs p-1.5 bg-background/50 rounded border border-border/30 truncate flex items-center justify-between text-muted-foreground select-all">
                      {isRevealed ? (
                        <span className="text-foreground/90 font-medium">{revealedSecrets[key]}</span>
                      ) : (
                        <span>••••••••••••••••</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Audit Logs */}
        <div className="lg:col-span-2 border border-border bg-card/50 rounded-xl p-6 shadow-sm flex flex-col gap-4">
          <div className="flex items-center gap-2 mb-2">
            <Activity className="w-5 h-5 text-blue-400" />
            <h2 className="text-lg font-bold">Agent Access Telegraph</h2>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-muted-foreground uppercase bg-muted/40 border-b border-border">
                <tr>
                  <th className="px-4 py-3 rounded-tl-lg font-medium">Timestamp</th>
                  <th className="px-4 py-3 font-medium">Agent Process</th>
                  <th className="px-4 py-3 font-medium">Worker Identity</th>
                  <th className="px-4 py-3 rounded-tr-lg font-medium">Keys Decrypted</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/50">
                {(!audit_logs || audit_logs.length === 0) ? (
                  <tr>
                    <td colSpan={4} className="px-4 py-6 text-center text-muted-foreground">
                      No security audit events captured in Elasticsearch yet.
                    </td>
                  </tr>
                ) : (
                  audit_logs.map((log, i) => (
                    <tr key={i} className="hover:bg-muted/10 transition-colors">
                      <td className="px-4 py-3 font-mono text-xs opacity-80 whitespace-nowrap">
                        {new Date(log['@timestamp']).toLocaleString()}
                      </td>
                      <td className="px-4 py-3">
                        <span className="px-2 py-0.5 rounded-full bg-primary/10 tracking-widest text-[10px] text-primary border border-primary/20 uppercase">
                          {log.agent_roles || 'System'}
                        </span>
                      </td>
                      <td className="px-4 py-3 font-medium">{log.worker_name}</td>
                      <td className="px-4 py-3 text-xs w-full">
                        <div className="flex flex-wrap gap-1">
                          {(log.keys_retrieved || []).map(k => (
                            <span key={k} className="px-1.5 py-0.5 bg-background border border-border rounded text-[10px] font-mono text-muted-foreground">
                              {k}
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* ── Modal for Add Secret (Guided Credential Type Flow) ──────────── */}
      {showAddModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-background/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md border border-border bg-card rounded-xl shadow-xl overflow-hidden flex flex-col p-6 animate-scale-in">
            <div className="flex items-center justify-between mb-5">
              <h3 className="text-lg font-bold text-foreground">Create New Vault Secret</h3>
              <button
                onClick={closeModal}
                className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleSaveSecret} className="space-y-5">
              {modalError && (
                <div className="p-3 border border-destructive/20 bg-destructive/5 rounded-lg text-destructive text-xs flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-shrink-0" />
                  <span>{modalError}</span>
                </div>
              )}

              {/* Step 1: Credential Type */}
              <div className="space-y-2">
                <label className="text-xs font-semibold text-muted-foreground tracking-wide uppercase">Credential Type</label>
                <div className="grid grid-cols-2 gap-3">
                  <button
                    type="button"
                    onClick={() => { setCredentialType('llm'); setSelectedProvider(''); }}
                    className={`flex flex-col items-center gap-2 p-4 rounded-xl border-2 transition-all ${credentialType === 'llm'
                        ? 'border-primary bg-primary/5 shadow-sm shadow-primary/10'
                        : 'border-border/50 bg-background/30 hover:border-border hover:bg-muted/30'
                      }`}
                  >
                    <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${credentialType === 'llm' ? 'bg-primary/15 text-primary' : 'bg-muted/60 text-muted-foreground'
                      }`}>
                      <Key className="w-4.5 h-4.5" />
                    </div>
                    <span className={`text-xs font-semibold ${credentialType === 'llm' ? 'text-foreground' : 'text-muted-foreground'}`}>
                      LLM Provider
                    </span>
                    <span className="text-[10px] text-muted-foreground/70 text-center leading-tight">
                      OpenAI, Anthropic, Gemini, xAI
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => { setCredentialType('repo'); setSelectedProvider(''); }}
                    className={`flex flex-col items-center gap-2 p-4 rounded-xl border-2 transition-all ${credentialType === 'repo'
                        ? 'border-primary bg-primary/5 shadow-sm shadow-primary/10'
                        : 'border-border/50 bg-background/30 hover:border-border hover:bg-muted/30'
                      }`}
                  >
                    <div className={`w-9 h-9 rounded-lg flex items-center justify-center ${credentialType === 'repo' ? 'bg-primary/15 text-primary' : 'bg-muted/60 text-muted-foreground'
                      }`}>
                      <Shield className="w-4.5 h-4.5" />
                    </div>
                    <span className={`text-xs font-semibold ${credentialType === 'repo' ? 'text-foreground' : 'text-muted-foreground'}`}>
                      Repo Provider
                    </span>
                    <span className="text-[10px] text-muted-foreground/70 text-center leading-tight">
                      GitHub, Azure DevOps
                    </span>
                  </button>
                </div>
              </div>

              {/* Step 2: Provider Dropdown (shown after credential type selected) */}
              {credentialType && (
                <div className="space-y-2 animate-fade-in">
                  <label className="text-xs font-semibold text-muted-foreground tracking-wide uppercase">
                    {credentialType === 'llm' ? 'LLM Provider' : 'Repository Provider'}
                  </label>
                  <div className="relative">
                    <select
                      id="provider-select"
                      value={selectedProvider}
                      onChange={(e) => setSelectedProvider(e.target.value)}
                      className="w-full p-2.5 bg-background border border-border rounded-lg text-sm outline-none focus:border-primary/50 appearance-none cursor-pointer pr-10"
                      required
                    >
                      <option value="" disabled>
                        Select a {credentialType === 'llm' ? 'frontier model provider' : 'repository platform'}...
                      </option>
                      {(credentialType === 'llm' ? LLM_PROVIDERS : REPO_PROVIDERS).map(p => (
                        <option key={p.id} value={p.id}>{p.label}</option>
                      ))}
                    </select>
                    <ChevronDown className="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none" />
                  </div>
                </div>
              )}

              {/* Step 3: Auto-generated Key Name (read-only display) */}
              {computedKeyName && (
                <div className="space-y-2 animate-fade-in">
                  <label className="text-xs font-semibold text-muted-foreground tracking-wide uppercase">Vault Key Name</label>
                  <div className="flex items-center gap-2 p-2.5 bg-muted/30 border border-border/50 rounded-lg">
                    <Key className="w-3.5 h-3.5 text-primary flex-shrink-0" />
                    <span className="font-mono text-sm font-semibold text-foreground">{computedKeyName}</span>
                    <span className="ml-auto text-[10px] text-muted-foreground/70 italic">Auto-resolved</span>
                  </div>
                </div>
              )}

              {/* Step 4: Secret Value (shown after provider selected) */}
              {selectedProvider && (
                <div className="space-y-2 animate-fade-in">
                  <label className="text-xs font-semibold text-muted-foreground tracking-wide uppercase">{getValueLabel()}</label>
                  <textarea
                    placeholder={getValuePlaceholder()}
                    value={modalValue}
                    onChange={(e) => setModalValue(e.target.value)}
                    rows={3}
                    className="w-full p-2.5 bg-background border border-border rounded-lg text-sm outline-none focus:border-primary/50 font-mono"
                    required
                  />
                </div>
              )}

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={closeModal}
                  className="px-4 py-2 border border-border rounded-lg text-xs font-semibold hover:bg-muted transition-all"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={modalSubmitting || !computedKeyName || !modalValue}
                  className="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-semibold hover:bg-primary/95 transition-all shadow-sm disabled:opacity-50"
                >
                  {modalSubmitting ? "Saving..." : "Save to Vault"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Modal for Edit Secret (simple key/value — key is immutable) ── */}
      {editSecretKey !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-background/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md border border-border bg-card rounded-xl shadow-xl overflow-hidden flex flex-col p-6 animate-scale-in">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold text-foreground">Edit Secret Value</h3>
              <button
                onClick={closeModal}
                className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <form onSubmit={handleSaveSecret} className="space-y-4">
              {modalError && (
                <div className="p-3 border border-destructive/20 bg-destructive/5 rounded-lg text-destructive text-xs flex gap-2">
                  <AlertCircle className="w-4 h-4 flex-shrink-0" />
                  <span>{modalError}</span>
                </div>
              )}

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-muted-foreground">Secret Key Name</label>
                <div className="flex items-center gap-2 p-2.5 bg-muted/30 border border-border/50 rounded-lg">
                  <Key className="w-3.5 h-3.5 text-primary flex-shrink-0" />
                  <span className="font-mono text-sm font-semibold text-foreground">{modalKey}</span>
                </div>
              </div>

              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-muted-foreground">Secret Plaintext Value</label>
                <textarea
                  placeholder="Enter the secure token/credentials..."
                  value={modalValue}
                  onChange={(e) => setModalValue(e.target.value)}
                  rows={4}
                  className="w-full p-2.5 bg-background border border-border rounded-lg text-sm outline-none focus:border-primary/50 font-mono"
                  required
                />
              </div>

              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={closeModal}
                  className="px-4 py-2 border border-border rounded-lg text-xs font-semibold hover:bg-muted transition-all"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={modalSubmitting}
                  className="px-4 py-2 bg-primary text-primary-foreground rounded-lg text-xs font-semibold hover:bg-primary/95 transition-all shadow-sm disabled:opacity-50"
                >
                  {modalSubmitting ? "Saving..." : "Save to Vault"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

