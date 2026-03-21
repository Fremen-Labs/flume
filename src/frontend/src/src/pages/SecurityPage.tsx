import { useEffect, useState } from 'react';
import { Shield, Key, Lock, EyeOff, Activity, AlertCircle } from 'lucide-react';
import { GlassMetricCard } from '@/components/GlassMetricCard';

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

  useEffect(() => {
    fetch('/api/security')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        setData(json);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

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
      {/* Header */}
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-bold tracking-tight text-foreground flex items-center gap-3">
          <Shield className="w-8 h-8 text-primary" />
          OpenBao Hive Security
        </h1>
        <p className="text-muted-foreground text-sm max-w-2xl">
          Real-time auditing of Flume vault retrievals and cryptographic bindings natively processed via Elasticsearch telemetry tracing.
        </p>
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
          subtitle="Vaulted Identity Certificates"
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
          <div className="flex items-center gap-2 mb-2">
            <Key className="w-5 h-5 text-primary" />
            <h2 className="text-lg font-bold">Active Certificates</h2>
          </div>
          {rootKeys.length === 0 ? (
            <div className="text-muted-foreground text-sm italic">No keys actively stored in `secret/flume`.</div>
          ) : (
            <div className="flex flex-col gap-3">
              {rootKeys.map((key) => (
                <div key={key} className="p-3 border border-border/50 rounded-lg flex items-center justify-between bg-background/30 backdrop-blur-sm">
                  <div className="font-mono text-sm">{key}</div>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <EyeOff className="w-3.5 h-3.5" />
                    <span className="opacity-75">Encrypted</span>
                  </div>
                </div>
              ))}
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
    </div>
  );
}
