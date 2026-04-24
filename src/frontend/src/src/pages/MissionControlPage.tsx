import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Radar, PowerOff, ShieldAlert, Loader2, Activity, Database, Zap, HardDrive } from 'lucide-react';
import { LiveMissionRadar } from '@/components/mission/LiveMissionRadar';
import { toast } from 'sonner';
import { useSystemState } from '@/hooks/useSystemState';

import { useEffect, useCallback, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchAgentModels, normalizeRoleSpec, RoleForm, SaveState, SETTINGS_DEFAULT_CREDENTIAL_ID } from '@/components/mission/AgentConfigPanel';


export default function MissionControlPage() {

  const queryClient = useQueryClient();
  const { data: cfg, isLoading: cfgLoading } = useQuery({
    queryKey: ['settings', 'agent-models'],
    queryFn: fetchAgentModels,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const [roleForms, setRoleForms] = useState<Record<string, RoleForm>>({});
  const [roleSaveState, setRoleSaveState] = useState<Record<string, SaveState>>({});
  const [roleSaveMsg, setRoleSaveMsg] = useState<Record<string, string>>({});
  const originalForms = useRef<Record<string, RoleForm>>({});

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
      setTimeout(() => setRoleSaveState((prev) => (prev[roleId] === 'success' ? { ...prev, [roleId]: 'idle' } : prev)), 4000);
    } catch (e) {
      setRoleSaveState((prev) => ({ ...prev, [roleId]: 'error' }));
      setRoleSaveMsg((prev) => ({ ...prev, [roleId]: e instanceof Error ? e.message : 'Save failed' }));
    }
  }, [roleForms, queryClient]);

  const [haltingState, setHaltingState] = useState<'idle' | 'confirm' | 'halting'>('idle');
  const systemState = useSystemState(2000);
  const telemetry = systemState?.telemetry || {};

  const triggerKillSwitch = async () => {
    setHaltingState('halting');
    try {
      const res = await fetch('/api/tasks/stop-all', {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer local-admin-1234'
        }
      });
      if (res.ok) {
        toast.success("Grid Halted Successfully", { description: "All autonomous workers and background threads have been forcefully terminated." });
      } else {
        toast.error("Kill Switch Failed", { description: "The Orchestrator refused the shutdown command." });
      }
    } catch(e) {
      toast.error("Grid Timeout", { description: "The API endpoint was unreachable natively." });
    }
    setHaltingState('idle');
  };

  return (
    <div className="p-5 lg:p-6 max-w-[1600px] mx-auto space-y-6 relative">
      <motion.div
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex items-center justify-between relative z-10"
      >
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-primary/15 flex items-center justify-center breathing">
            <Radar className="w-5 h-5 text-primary icon-glow-active" />
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-tight text-foreground">Mission Control</h1>
            <p className="text-xs text-muted-foreground">Live autonomous delivery operations</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          
          <AnimatePresence mode="wait">
            {haltingState === 'idle' && (
              <motion.button
                key="idle"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                onClick={() => setHaltingState('confirm')}
                className="glass-card border-destructive/30 hover:bg-destructive/10 px-4 py-1.5 flex items-center gap-2 text-xs text-destructive hover:text-red-400 transition-colors font-medium rounded-md group"
              >
                <PowerOff className="w-3.5 h-3.5 group-hover:scale-110 transition-transform" />
                Halt Grid
              </motion.button>
            )}
            
            {haltingState === 'confirm' && (
              <motion.div
                key="confirm"
                initial={{ opacity: 0, scale: 0.95, x: 20 }}
                animate={{ opacity: 1, scale: 1, x: 0 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="flex items-center gap-2"
              >
                <button
                  onClick={triggerKillSwitch}
                  className="bg-destructive hover:bg-red-600 text-white px-4 py-1.5 flex items-center gap-2 text-xs font-bold rounded-md shadow-[0_0_15px_rgba(239,68,68,0.4)] transition-colors"
                >
                  <ShieldAlert className="w-3.5 h-3.5" /> Confirm Terminate?
                </button>
                <button
                  onClick={() => setHaltingState('idle')}
                  className="px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
              </motion.div>
            )}

            {haltingState === 'halting' && (
              <motion.div
                key="halting"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                className="glass-card border-destructive/50 bg-destructive/10 px-4 py-1.5 flex items-center gap-2 text-xs text-destructive font-medium rounded-md"
              >
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
                Terminating Threads...
              </motion.div>
            )}
          </AnimatePresence>

          <div className="w-px h-5 bg-white/10 mx-2" />

          <div className="glass-card px-3 py-1.5 flex items-center gap-2 text-xs">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-success opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-success" />
            </span>
            <span className="text-foreground font-medium">System Online</span>
          </div>
        </div>
      </motion.div>

      {Object.keys(telemetry).length > 0 && (
        <motion.div 
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          className="grid grid-cols-2 md:grid-cols-4 gap-4 relative z-10"
        >
          <div className="glass-card p-4 flex flex-col gap-1">
            <div className="flex items-center gap-2 text-muted-foreground text-xs font-semibold uppercase tracking-wider">
              <Activity className="w-3.5 h-3.5 text-blue-400" />
              Agent Throughput
            </div>
            <div className="text-2xl font-bold font-mono text-white flex items-baseline gap-1">
              {telemetry.completedWork || 0} <span className="text-xs font-sans text-muted-foreground font-normal">tasks solved</span>
            </div>
          </div>
          <div className="glass-card p-4 flex flex-col gap-1">
            <div className="flex items-center gap-2 text-muted-foreground text-xs font-semibold uppercase tracking-wider">
              <Zap className="w-3.5 h-3.5 text-yellow-400" />
              LLM Latency
            </div>
            <div className="text-2xl font-bold font-mono text-white">
              {telemetry.llmLatency || "---"}
            </div>
          </div>
          <div className="glass-card p-4 flex flex-col gap-1">
            <div className="flex items-center gap-2 text-muted-foreground text-xs font-semibold uppercase tracking-wider">
              <Database className="w-3.5 h-3.5 text-green-400" />
              Worktree Status (ES)
            </div>
            <div className="text-2xl font-bold font-mono text-white flex items-baseline gap-1">
              {telemetry.elasticAstCount || 0} <span className="text-xs font-sans text-muted-foreground font-normal">nodes</span>
            </div>
          </div>
          <div className="glass-card p-4 flex flex-col gap-1">
            <div className="flex items-center gap-2 text-muted-foreground text-xs font-semibold uppercase tracking-wider">
              <HardDrive className="w-3.5 h-3.5 text-purple-400" />
              OpenBao KMS
            </div>
            <div className="text-lg font-bold font-mono text-white flex items-center h-full">
              {telemetry.vaultSealed ? <span className="text-destructive drop-shadow-[0_0_8px_rgba(239,68,68,0.8)]">LOCKED</span> : <span className="text-success drop-shadow-[0_0_8px_rgba(34,197,94,0.8)]">ACTIVE</span>}
            </div>
          </div>
        </motion.div>
      )}

      <div className="relative z-10">
        
          <LiveMissionRadar 
            cfg={cfg}
            roleForms={roleForms}
            roleSaveState={roleSaveState}
            roleSaveMsg={roleSaveMsg}
            updateRoleForm={updateRoleForm}
            saveRole={saveRole}
            resetRole={resetRole}
          />

      </div>
    </div>
  );
}