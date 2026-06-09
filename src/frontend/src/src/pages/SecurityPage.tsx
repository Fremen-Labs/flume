import React, { useEffect, useState } from 'react';
import { Shield, Key, Lock, Unlock, Eye, EyeOff, Activity, AlertCircle, Copy, Check, Plus, Trash2, Edit2, X } from 'lucide-react';
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

  const handleSaveSecret = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!modalKey || !modalValue) {
      setModalError('Key and value are required');
      return;
    }
    setModalSubmitting(true);
    setModalError(null);
    try {
      const res = await fetch('/api/security/secrets/update', {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ key: modalKey, value: modalValue }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `HTTP ${res.status}`);
      }
      fetchSecurityData();
      setShowAddModal(false);
      setEditSecretKey(null);
      setModalKey('');
      setModalValue('');
    } catch (err: any) {
      setModalError(err.message);
    } finally {
      setModalSubmitting(false);
    }
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
              className={`p-1 rounded-md flex-shrink-0 transition-all active:scale-95 disabled:opacity-50 ${
                validatingToken ? 'animate-pulse' : ''
              } ${
                tokenValid === true
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

      {/* Modal for Add/Edit Secret */}
      {(showAddModal || editSecretKey !== null) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-background/80 backdrop-blur-sm animate-fade-in">
          <div className="w-full max-w-md border border-border bg-card rounded-xl shadow-xl overflow-hidden flex flex-col p-6 animate-scale-in">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold text-foreground">
                {showAddModal ? "Create New Vault Secret" : `Edit Secret Value`}
              </h3>
              <button
                onClick={() => {
                  setShowAddModal(false);
                  setEditSecretKey(null);
                  setModalKey('');
                  setModalValue('');
                  setModalError(null);
                }}
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
                <input
                  type="text"
                  placeholder="e.g. OPENAI_API_KEY"
                  value={modalKey}
                  onChange={(e) => setModalKey(e.target.value.toUpperCase())}
                  disabled={editSecretKey !== null} // Key is immutable on edit
                  className="w-full p-2.5 bg-background border border-border rounded-lg text-sm outline-none focus:border-primary/50 disabled:opacity-50"
                  required
                />
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
                  onClick={() => {
                    setShowAddModal(false);
                    setEditSecretKey(null);
                    setModalKey('');
                    setModalValue('');
                    setModalError(null);
                  }}
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
