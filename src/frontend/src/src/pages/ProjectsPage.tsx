import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import {
  Search, FolderOpen, Plus, Loader2, AlertCircle, Trash2,
  GitBranch, Download, CheckCircle2, XCircle, Clock, Zap,
  ArrowRight, Layers,
} from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { useSnapshot } from '@/hooks/useSnapshot';
import { FileExplorerModal } from '@/components/FileExplorerModal';
import type { ApiProject, Snapshot } from '@/types';
import projBg1 from '@/assets/projects/proj-bg-1.jpg';
import projBg2 from '@/assets/projects/proj-bg-2.jpg';
import projBg3 from '@/assets/projects/proj-bg-3.jpg';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';

const projectBgs = [projBg1, projBg2, projBg3];

function timeAgo(ts: string) {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

type CloneStatus = 'cloning' | 'pending' | 'cloned' | 'local' | 'no_repo' | 'failed' | 'unknown';

interface CloneStatusBadgeProps {
  status: CloneStatus;
}

function CloneStatusBadge({ status }: CloneStatusBadgeProps) {
  if (status === 'cloning' || status === 'pending') {
    return (
      <span className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-amber-500/10 border border-amber-500/20 text-amber-400 font-medium shrink-0">
        <Loader2 className="w-2.5 h-2.5 animate-spin" />
        Cloning…
      </span>
    );
  }
  if (status === 'cloned') {
    return (
      <span className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 font-medium shrink-0">
        <CheckCircle2 className="w-2.5 h-2.5" />
        Cloned
      </span>
    );
  }
  if (status === 'local') {
    return (
      <span className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-sky-500/10 border border-sky-500/20 text-sky-400 font-medium shrink-0">
        <GitBranch className="w-2.5 h-2.5" />
        Local
      </span>
    );
  }
  if (status === 'failed') {
    return (
      <span className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-destructive/10 border border-destructive/20 text-destructive font-medium shrink-0">
        <XCircle className="w-2.5 h-2.5" />
        Failed
      </span>
    );
  }
  return null;
}

export default function ProjectsPage() {
  const navigate = useNavigate();
  const { data: snapshot, isLoading, error } = useSnapshot();
  const qc = useQueryClient();
  const [search, setSearch] = useState('');
  const [explorerProject, setExplorerProject] = useState<ApiProject | null>(null);

  // ── Create project modal state ──────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createRepoUrl, setCreateRepoUrl] = useState('');
  const [createError, setCreateError] = useState('');
  const [createLoading, setCreateLoading] = useState(false);

  const projects = snapshot?.projects ?? [];
  const tasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];

  const filtered = projects.filter(p =>
    !search || p.name.toLowerCase().includes(search.toLowerCase()),
  );

  // Derive task stats per project from real tasks
  function projectStats(projectId: string) {
    const ptasks = tasks.filter(t => t.repo === projectId);
    const activeWorkers = workers.filter(
      w => w.current_task_id && ptasks.some(t => t.id === w.current_task_id),
    );
    return {
      total: ptasks.length,
      running: ptasks.filter(t => t.status === 'running').length,
      planned: ptasks.filter(t => t.status === 'planned' || t.status === 'ready' || t.status === 'inbox').length,
      done: ptasks.filter(t => t.status === 'done').length,
      blocked: ptasks.filter(t => t.status === 'blocked').length,
      agents: activeWorkers.length,
    };
  }

  async function createProject() {
    setCreateError('');
    const name = createName.trim();
    const repoUrl = createRepoUrl.trim();

    if (!name) {
      setCreateError('Project name is required');
      return;
    }

    setCreateLoading(true);
    try {
      const body: { name: string; repoUrl?: string } = { name };
      if (repoUrl) body.repoUrl = repoUrl;

      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000);
      const res = await fetch('/api/projects', {
        signal: controller.signal,
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      clearTimeout(timeoutId);

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const parts = [data.error, data.detail, data.hint].filter(
          (x: unknown) => typeof x === 'string' && x.trim(),
        ) as string[];
        throw new Error(parts.length ? parts.join('\n\n') : 'Failed to create project');
      }

      // Optimistically insert the new project into the snapshot cache
      if (data.project) {
        qc.setQueryData<Snapshot>(['snapshot'], (old) => {
          if (!old) return old;
          return { ...old, projects: [...old.projects, data.project as ApiProject] };
        });
      } else {
        qc.invalidateQueries({ queryKey: ['snapshot'] });
      }

      setCreateOpen(false);
      setCreateName('');
      setCreateRepoUrl('');
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : 'Failed to create project');
    } finally {
      setCreateLoading(false);
    }
  }

  async function deleteProject(projectId: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!window.confirm('Delete this project? This cannot be undone.')) return;
    try {
      qc.setQueryData<Snapshot>(['snapshot'], (old) => {
        if (!old) return old;
        return { ...old, projects: old.projects.filter((p) => p.id !== projectId) };
      });
      await fetch(`/api/projects/${encodeURIComponent(projectId)}/delete`, { method: 'POST' });
      qc.invalidateQueries({ queryKey: ['snapshot'] });
    } catch {
      qc.invalidateQueries({ queryKey: ['snapshot'] });
    }
  }

  const cloningCount = projects.filter(p => {
    const cs = (p as ApiProject & { clone_status?: string }).clone_status;
    return cs === 'cloning' || cs === 'pending';
  }).length;

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">

      {/* ── Page header ── */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <div className="flex items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground flex items-center gap-2">
              <Layers className="w-6 h-6 text-primary/70" />
              Projects
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              {isLoading
                ? 'Loading…'
                : `${projects.length} project${projects.length !== 1 ? 's' : ''} · AI-driven software delivery`}
              {cloningCount > 0 && (
                <span className="ml-2 inline-flex items-center gap-1 text-amber-400">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  {cloningCount} cloning
                </span>
              )}
            </p>
          </div>
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-semibold hover:bg-primary/90 transition-all shadow-lg shadow-primary/20 hover:shadow-primary/30 hover:-translate-y-0.5 active:translate-y-0"
          >
            <Plus className="w-4 h-4" />
            New Project
          </button>
        </div>
      </motion.div>

      {/* ── Search row ── */}
      <div className="relative z-10 max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground/60" />
        <input
          type="text"
          placeholder="Search projects…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full pl-10 pr-4 py-2.5 text-sm rounded-xl glass-surface text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:ring-1 focus:ring-primary/50 transition-all"
        />
      </div>

      {/* ── Loading ── */}
      {isLoading && (
        <div className="flex items-center justify-center py-24 gap-3 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="text-sm">Loading projects…</span>
        </div>
      )}

      {/* ── Error ── */}
      {error && (
        <div className="flex items-center gap-3 p-4 rounded-xl bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          Failed to connect to backend. Is server.py running on port 8765?
        </div>
      )}

      {/* ── Project Grid ── */}
      {!isLoading && !error && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5 relative z-10">
          <AnimatePresence mode="popLayout">
            {filtered.map((project, i) => {
              const stats = projectStats(project.id);
              const cloneStatus = ((project as ApiProject & { clone_status?: string }).clone_status ?? 'unknown') as CloneStatus;
              const isCloning = cloneStatus === 'cloning' || cloneStatus === 'pending';
              const hasFailed = cloneStatus === 'failed';
              const isReady = cloneStatus === 'cloned' || cloneStatus === 'local';

              return (
                <motion.div
                  key={project.id}
                  initial={{ opacity: 0, y: 20, scale: 0.97 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ delay: i * 0.06, duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
                  whileHover={{ y: -5, transition: { duration: 0.2, ease: 'easeOut' } }}
                  onClick={() => navigate(`/projects/${encodeURIComponent(project.id)}`)}
                  className="glass-card shadow-xl shadow-black/40 ring-1 ring-white/10 cursor-pointer group relative overflow-hidden"
                  style={{ '--glow-color': isCloning ? 'rgba(245,158,11,0.15)' : hasFailed ? 'rgba(239,68,68,0.15)' : 'rgba(var(--primary-rgb),0.12)' } as React.CSSProperties}
                >
                  {/* Background image */}
                  <div className="absolute inset-0 pointer-events-none">
                    <img
                      src={projectBgs[i % projectBgs.length]}
                      alt=""
                      className="w-full h-full object-cover opacity-[0.07] group-hover:opacity-[0.14] transition-opacity duration-700"
                    />
                    <div className="absolute inset-0 bg-gradient-to-t from-card via-card/95 to-card/50" />
                  </div>

                  {/* Hover shimmer */}
                  <div
                    className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none z-[1]"
                    style={{ background: 'linear-gradient(135deg, rgba(255,255,255,0.035) 0%, transparent 60%)' }}
                  />

                  {/* Cloning pulse ring */}
                  {isCloning && (
                    <div className="absolute inset-0 rounded-[inherit] ring-1 ring-amber-500/30 pointer-events-none z-[2]">
                      <div className="absolute inset-0 rounded-[inherit] ring-1 ring-amber-500/20 animate-ping" />
                    </div>
                  )}

                  {/* Card content */}
                  <div className="relative p-5 z-[3]">
                    {/* Header row */}
                    <div className="flex items-start justify-between gap-3 mb-4">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap mb-1">
                          <h3 className="text-sm font-semibold text-foreground group-hover:text-primary transition-colors truncate">
                            {project.name}
                          </h3>
                          <CloneStatusBadge status={cloneStatus} />
                        </div>
                        {project.repoUrl && (
                          <p className="text-[10px] text-muted-foreground/50 truncate font-mono">
                            {project.repoUrl.replace(/^https?:\/\/[^@]*@/, 'https://')}
                          </p>
                        )}
                      </div>
                      <div className="shrink-0 text-right">
                        <div className="text-xl font-bold tabular-nums text-foreground">{stats.total}</div>
                        <div className="text-[10px] text-muted-foreground/60">tasks</div>
                      </div>
                    </div>

                    {/* Clone progress bar */}
                    {isCloning && (
                      <div className="mb-4">
                        <div className="flex items-center gap-2 text-[11px] text-amber-400/80 mb-1.5">
                          <Download className="w-3 h-3" />
                          Cloning repository in the background…
                        </div>
                        <div className="h-1 rounded-full bg-white/5 overflow-hidden">
                          <div className="h-full bg-gradient-to-r from-amber-500/60 to-amber-400/80 rounded-full animate-[progressPulse_2s_ease-in-out_infinite]" style={{ width: '65%' }} />
                        </div>
                      </div>
                    )}

                    {/* Clone failed note */}
                    {hasFailed && (
                      <div className="mb-4 flex items-center gap-1.5 text-[11px] text-destructive/80 bg-destructive/5 border border-destructive/10 rounded-lg px-2.5 py-1.5">
                        <XCircle className="w-3 h-3 shrink-0" />
                        Clone failed — click to view details
                      </div>
                    )}

                    {/* Stats grid */}
                    <div className="grid grid-cols-4 gap-1.5 mb-4">
                      {[
                        { label: 'Running', value: stats.running, color: 'text-primary' },
                        { label: 'Planned', value: stats.planned, color: 'text-muted-foreground' },
                        { label: 'Done', value: stats.done, color: 'text-emerald-400' },
                        { label: 'Blocked', value: stats.blocked, color: 'text-destructive' },
                      ].map(s => (
                        <div key={s.label} className="bg-white/[0.03] border border-white/[0.06] rounded-lg py-2 text-center">
                          <div className={`text-sm font-bold tabular-nums ${s.color}`}>{s.value}</div>
                          <div className="text-[9px] text-muted-foreground/60 uppercase tracking-wide">{s.label}</div>
                        </div>
                      ))}
                    </div>

                    {/* Footer row */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-1.5">
                        {stats.agents > 0 ? (
                          <span className="flex items-center gap-1 text-[10px] text-primary/70">
                            <Zap className="w-3 h-3" />
                            {stats.agents} agent{stats.agents !== 1 ? 's' : ''} active
                          </span>
                        ) : (
                          <span className="text-[10px] text-muted-foreground/40 flex items-center gap-1">
                            <Clock className="w-3 h-3" />
                            {timeAgo(project.created_at)}
                          </span>
                        )}
                      </div>

                      <div className="flex items-center gap-1.5" onClick={e => e.stopPropagation()}>
                        {isReady && (
                          <button
                            onClick={e => { e.stopPropagation(); setExplorerProject(project); }}
                            className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded-lg bg-sky-500/10 border border-sky-500/20 text-sky-400 hover:bg-sky-500/20 hover:border-sky-500/40 transition-all font-medium"
                          >
                            <FolderOpen className="w-3 h-3" />
                            Browse
                          </button>
                        )}
                        <button
                          onClick={e => navigate(`/projects/${encodeURIComponent(project.id)}`)}
                          className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded-lg bg-white/5 border border-white/10 text-muted-foreground hover:text-primary hover:border-primary/30 transition-all font-medium"
                        >
                          Open
                          <ArrowRight className="w-3 h-3" />
                        </button>
                        <button
                          onClick={e => deleteProject(project.id, e)}
                          className="flex items-center gap-0.5 text-[10px] p-1.5 rounded-lg text-muted-foreground/30 hover:text-destructive hover:bg-destructive/10 transition-all"
                          title="Delete project"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>

          {/* ── Empty state ── */}
          {filtered.length === 0 && !isLoading && (
            <motion.div
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              className="col-span-full"
            >
              {search ? (
                <div className="glass-card p-12 text-center">
                  <Search className="w-8 h-8 mx-auto mb-3 opacity-20" />
                  <p className="text-sm text-muted-foreground">No projects match &ldquo;{search}&rdquo;</p>
                  <button onClick={() => setSearch('')} className="mt-3 text-xs text-primary hover:underline">
                    Clear search
                  </button>
                </div>
              ) : (
                <div className="glass-card p-12 text-center relative overflow-hidden">
                  {/* Decorative gradient */}
                  <div className="absolute inset-0 bg-gradient-to-br from-primary/5 via-transparent to-sky-500/5 pointer-events-none" />
                  <div className="relative">
                    <div className="w-16 h-16 mx-auto mb-5 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center">
                      <Layers className="w-8 h-8 text-primary/60" />
                    </div>
                    <h3 className="text-base font-semibold text-foreground mb-2">No projects yet</h3>
                    <p className="text-sm text-muted-foreground max-w-sm mx-auto mb-6">
                      Connect a Git repository to start your AI-driven workflow. Agents will clone, analyze, and deliver features automatically.
                    </p>
                    <button
                      onClick={() => setCreateOpen(true)}
                      className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-semibold hover:bg-primary/90 transition-all shadow-lg shadow-primary/20"
                    >
                      <Plus className="w-4 h-4" />
                      Create your first project
                    </button>
                  </div>
                </div>
              )}
            </motion.div>
          )}
        </div>
      )}

      {/* ── File Explorer Modal ── */}
      {explorerProject && (
        <FileExplorerModal
          open={!!explorerProject}
          onOpenChange={open => { if (!open) setExplorerProject(null); }}
          projectName={explorerProject.name}
          projectId={explorerProject.id}
        />
      )}

      {/* ── Create Project Dialog ── */}
      <Dialog
        open={createOpen}
        onOpenChange={v => {
          setCreateOpen(v);
          if (!v) {
            setCreateError('');
            setCreateLoading(false);
          }
        }}
      >
        <DialogContent className="bg-[#0a0f1a] border-white/10 text-foreground max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Plus className="w-4 h-4 text-primary" />
              Create Project
            </DialogTitle>
            <DialogDescription className="text-muted-foreground/70">
              Connect a Git repository — agents will clone it into the shared workspace for AI analysis and task delivery.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-1">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Project name <span className="text-destructive">*</span></label>
              <Input
                value={createName}
                onChange={e => setCreateName(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && createProject()}
                placeholder="e.g. customer-onboarding"
                autoFocus
                className="bg-white/5 border-white/10 focus:border-primary/40"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">Repository URL <span className="text-muted-foreground/50">(optional)</span></label>
              <Input
                value={createRepoUrl}
                onChange={e => setCreateRepoUrl(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && createProject()}
                placeholder="https://github.com/org/repo.git"
                type="url"
                className="bg-white/5 border-white/10 focus:border-primary/40 font-mono text-xs"
              />
              <p className="text-[10px] text-muted-foreground/50">
                Paste an HTTPS clone URL. Credentials are resolved from your stored tokens.
              </p>
            </div>

            {createError && (
              <div className="flex items-start gap-2 text-destructive text-xs p-3 rounded-lg bg-destructive/5 border border-destructive/20">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                <span className="whitespace-pre-wrap break-words min-w-0">{createError}</span>
              </div>
            )}
          </div>

          <DialogFooter className="gap-2 sm:gap-2">
            <button
              onClick={() => setCreateOpen(false)}
              className="px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
              disabled={createLoading}
            >
              Cancel
            </button>
            <button
              onClick={createProject}
              disabled={createLoading}
              className="flex items-center gap-2 px-5 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-semibold hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed transition-all shadow-md shadow-primary/20"
            >
              {createLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              {createLoading ? 'Creating…' : 'Create Project'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
