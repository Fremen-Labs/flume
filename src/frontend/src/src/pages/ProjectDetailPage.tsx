import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  ArrowLeft, ChevronRight, ChevronDown, FolderOpen,
  GitBranch, GitCommit, GitPullRequest, Loader2, AlertCircle,
  ExternalLink, Play, Square, RefreshCw, Plus, Unlink,
  CheckSquare, Trash2, Archive, X as XIcon,
} from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';
import { useAgentStatus, useAgentControls } from '@/hooks/useAgentStatus';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { StatusBadge } from '@/components/StatusBadge';
import { FileExplorerModal } from '@/components/FileExplorerModal';
import { IntakeModal } from '@/components/IntakeModal';
import type { ApiTask } from '@/types';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';

function timeAgo(ts?: string) {
  if (!ts) return '';
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

async function fetchTaskApiJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  const data: unknown = await r.json().catch(() => ({}));
  if (!r.ok) {
    const msg =
      typeof data === 'object' &&
      data !== null &&
      'error' in data &&
      typeof (data as { error: unknown }).error === 'string'
        ? (data as { error: string }).error
        : `Request failed (${r.status})`;
    throw new Error(msg);
  }
  return data as T;
}

// Build epic > feature > story > task hierarchy from flat list
interface TaskNode extends ApiTask { children: TaskNode[]; }

function buildHierarchy(tasks: ApiTask[]): TaskNode[] {
  const byId = new Map(tasks.map(t => [t.id, { ...t, children: [] as TaskNode[] }]));
  const typeOrder = ['epic', 'feature', 'story', 'task'] as const;
  const roots: TaskNode[] = [];

  for (const t of tasks) {
    const node = byId.get(t.id)!;

    // Prefer explicit parent_id for placement (new sequential-task model)
    const parentId = (t as ApiTask & { parent_id?: string }).parent_id;
    if (parentId) {
      const parent = byId.get(parentId);
      if (parent) {
        parent.children.push(node);
        continue;
      }
    }

    // Legacy fallback: use depends_on to infer the parent by type
    if (!t.item_type || !t.depends_on?.length) {
      if (t.item_type === 'epic' || !t.item_type) roots.push(node);
      continue;
    }
    const typeIdx = typeOrder.indexOf(t.item_type as typeof typeOrder[number]);
    const parentType = typeIdx > 0 ? typeOrder[typeIdx - 1] : null;
    let placed = false;
    for (const dep of t.depends_on) {
      const parent = byId.get(dep);
      if (parent && (!parentType || parent.item_type === parentType)) {
        parent.children.push(node);
        placed = true;
        break;
      }
    }
    if (!placed) roots.push(node);
  }
  return roots;
}

// Collect node ID + all descendant IDs for cascade operations
function collectIds(node: TaskNode): string[] {
  return [node.id, ...node.children.flatMap(collectIds)];
}

// ─── Agent Controls strip ─────────────────────────────────────────────────────

function AgentControls() {
  const { data: status, isLoading } = useAgentStatus();
  const { start, stop } = useAgentControls();
  const qc = useQueryClient();

  const isRunning = status?.running ?? false;
  const busy = start.isPending || stop.isPending;

  return (
    <div className="flex items-center gap-2">
      {isLoading ? (
        <Loader2 className="w-3.5 h-3.5 animate-spin text-muted-foreground" />
      ) : (
        <>
          <span className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
            <span className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-success animate-pulse' : 'bg-muted-foreground/40'}`} />
            {isRunning ? 'Agents running' : 'Agents stopped'}
          </span>
          {!isRunning ? (
            <button
              onClick={() => start.mutate()}
              disabled={busy}
              className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded-md bg-success/10 border border-success/20 text-success hover:bg-success/20 disabled:opacity-50 transition-all"
            >
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
              Start
            </button>
          ) : (
            <button
              onClick={() => stop.mutate()}
              disabled={busy}
              className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded-md bg-destructive/10 border border-destructive/20 text-destructive hover:bg-destructive/20 disabled:opacity-50 transition-all"
            >
              {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Square className="w-3 h-3" />}
              Stop
            </button>
          )}
          <button
            onClick={() => {
              qc.invalidateQueries({ queryKey: ['agent-status'] });
              qc.invalidateQueries({ queryKey: ['snapshot'] });
            }}
            className="p-1 text-muted-foreground/50 hover:text-muted-foreground rounded transition-colors"
            title="Refresh"
          >
            <RefreshCw className="w-3 h-3" />
          </button>
        </>
      )}
    </div>
  );
}

// ─── Git info strip ───────────────────────────────────────────────────────────

function GitInfoStrip({ projectId }: { projectId: string }) {
  const { data: snapshot } = useSnapshot();
  const repo = snapshot?.repos?.find(r => r.id === projectId);

  if (!repo?.is_git) return null;

  return (
    <div className="flex items-center gap-4 text-[11px] text-muted-foreground mt-1.5 flex-wrap">
      {repo.current_branch && (
        <span className="flex items-center gap-1.5">
          <GitBranch className="w-3 h-3 text-primary/70" />
          <code className="text-primary/80 font-mono">{repo.current_branch}</code>
        </span>
      )}
      {repo.last_commit && (
        <span className="flex items-center gap-1.5">
          <GitCommit className="w-3 h-3 text-muted-foreground/60" />
          <code className="text-muted-foreground/60 font-mono">{repo.last_commit.hash.slice(0, 7)}</code>
          <span className="text-muted-foreground/50 truncate max-w-[300px]">{repo.last_commit.subject}</span>
          <span className="text-muted-foreground/40">· {repo.last_commit.author} · {timeAgo(repo.last_commit.date)}</span>
        </span>
      )}
    </div>
  );
}

function RepoBranchesManager({
  projectId,
  currentBranch,
}: {
  projectId: string;
  currentBranch?: string;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [defaultBranch, setDefaultBranch] = useState<string>('');
  const [branches, setBranches] = useState<string[]>([]);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [forceDelete, setForceDelete] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function refreshBranches() {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`/api/repos/${encodeURIComponent(projectId)}/branches`);
      const data = await r.json();
      if (!r.ok) throw new Error(data?.error ?? 'Failed to load branches');
      setDefaultBranch(data?.default ?? '');
      setBranches(data?.branches ?? []);
      setSelected(new Set());
      setConfirmDelete(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load branches');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!projectId) return;
    refreshBranches();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    // If force is disabled, ensure we don't keep protected branches selected.
    if (forceDelete) return;
    const protectedSet = new Set<string>();
    if (defaultBranch) protectedSet.add(defaultBranch);
    if (currentBranch) protectedSet.add(currentBranch);

    setSelected(prev => {
      if (prev.size === 0) return prev;
      const next = new Set<string>();
      for (const b of prev) {
        if (!protectedSet.has(b)) next.add(b);
      }
      return next;
    });
    setConfirmDelete(false);
  }, [forceDelete, defaultBranch, currentBranch]);

  function toggleSelected(branch: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(branch)) next.delete(branch);
      else next.add(branch);
      return next;
    });
  }

  const deletingCount = selected.size;
  const hasBranches = branches.length > 0;

  const protectedBranches = new Set<string>();
  if (defaultBranch && !forceDelete) protectedBranches.add(defaultBranch);
  if (currentBranch && !forceDelete) protectedBranches.add(currentBranch);

  async function deleteSelectedBranches() {
    if (selected.size === 0) return;
    setDeleting(true);
    setError(null);
    try {
      const r = await fetch(`/api/repos/${encodeURIComponent(projectId)}/branches/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          branches: [...selected],
          force: forceDelete,
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data?.error ?? 'Failed to delete branches');

      // Some branches may be skipped by safety rules; still refresh.
      setSelected(new Set());
      setConfirmDelete(false);
      await refreshBranches();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete branches');
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="glass-card p-5 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-foreground">Local Branches</h2>
          <p className="text-[10px] text-muted-foreground mt-1">
            {loading ? 'Loading…' : `${branches.length} local ${branches.length === 1 ? 'branch' : 'branches'}`}
            {defaultBranch ? ` · default: ${defaultBranch}` : ''}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={refreshBranches}
            disabled={loading || deleting}
            className="p-1.5 text-muted-foreground/60 hover:text-muted-foreground rounded transition-colors hover:bg-white/5 disabled:opacity-50"
            title="Refresh branches"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-[11px] px-3 py-2 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive">
          <AlertCircle className="w-4 h-4" />
          <span className="truncate">{error}</span>
        </div>
      )}

      {hasBranches ? (
        <div className="border border-border/50 rounded-lg overflow-hidden">
          <div className="max-h-56 overflow-y-auto">
            {branches.map(b => {
              const isDefault = b === defaultBranch;
              const isCurrent = b === currentBranch;
              const isProtected = protectedBranches.has(b);
              return (
                <div
                  key={b}
                  className="flex items-center gap-2 px-3 py-2 border-b border-border/30 last:border-b-0"
                >
                  <input
                    type="checkbox"
                    checked={selected.has(b)}
                    disabled={isProtected}
                    onChange={() => toggleSelected(b)}
                    className="h-3.5 w-3.5 rounded border-border/60 text-primary focus:ring-primary disabled:opacity-50 disabled:cursor-not-allowed"
                    aria-label={`Select ${b}`}
                  />
                  <GitBranch className="w-3.5 h-3.5 text-primary/60 shrink-0" />
                  <code className="text-[11px] font-mono text-muted-foreground truncate flex-1">{b}</code>
                  {isDefault && (
                    <span className="text-[10px] px-2 py-0.5 rounded bg-primary/10 border border-primary/20 text-primary shrink-0">
                      default
                    </span>
                  )}
                  {isCurrent && (
                    <span className="text-[10px] px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/20 text-amber-400 shrink-0">
                      current
                    </span>
                  )}
                  {isProtected && (
                    <span className="text-[10px] text-muted-foreground/60 shrink-0">protected</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        !loading && <p className="text-[11px] text-muted-foreground">No local branches found.</p>
      )}

      <div className="flex items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-[11px] text-muted-foreground select-none">
          <input
            type="checkbox"
            checked={forceDelete}
            onChange={e => {
              setForceDelete(e.target.checked);
              setSelected(new Set());
              setConfirmDelete(false);
            }}
            className="h-3.5 w-3.5 rounded border-border/60 text-primary focus:ring-primary"
          />
          Force delete (allows deleting default/current, and overrides task protection)
        </label>

        {deletingCount > 0 ? (
          <div className="flex items-center gap-2 shrink-0">
            {!confirmDelete ? (
              <button
                onClick={() => setConfirmDelete(true)}
                disabled={deleting}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive hover:bg-destructive/20 transition-all disabled:opacity-50"
              >
                <Trash2 className="w-3.5 h-3.5" /> Delete {deletingCount}
              </button>
            ) : (
              <>
                <button
                  onClick={deleteSelectedBranches}
                  disabled={deleting}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-destructive/20 border border-destructive/40 text-destructive hover:bg-destructive/30 transition-all font-semibold disabled:opacity-50"
                >
                  {deleting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                  Confirm Delete
                </button>
                <button
                  onClick={() => setConfirmDelete(false)}
                  disabled={deleting}
                  className="text-xs px-2 py-1.5 text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
              </>
            )}
            <button
              onClick={() => {
                setSelected(new Set());
                setConfirmDelete(false);
              }}
              disabled={deleting}
              className="p-1.5 text-muted-foreground/50 hover:text-muted-foreground rounded transition-colors disabled:opacity-50"
              title="Clear selection"
            >
              <XIcon className="w-3.5 h-3.5" />
            </button>
          </div>
        ) : (
          <span className="text-[10px] text-muted-foreground/70">Select branches to delete</span>
        )}
      </div>
    </div>
  );
}

function LocalBranchesDialog({
  projectId,
  currentBranch,
}: {
  projectId: string;
  currentBranch?: string;
}) {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-muted-foreground hover:text-primary hover:bg-white/10 hover:border-primary/30 transition-all font-medium"
        >
          <GitBranch className="w-3.5 h-3.5 text-primary/70" />
          Local Branches
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-5xl p-0 overflow-hidden">
        <div className="px-5 pt-5 pb-2 shrink-0">
          <DialogHeader>
            <DialogTitle>Local Branches</DialogTitle>
            <DialogDescription>Manage and delete local branches for this repository.</DialogDescription>
          </DialogHeader>
        </div>
        <div className="max-h-[80vh] overflow-y-auto pr-2 pb-5">
          <RepoBranchesManager projectId={projectId} currentBranch={currentBranch} />
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const projectId = decodeURIComponent(id ?? '');

  const { data: snapshot, isLoading, error } = useSnapshot();
  const currentBranch = snapshot?.repos?.find(r => r.id === projectId)?.current_branch;
  const [explorerOpen, setExplorerOpen] = useState(false);
  const [intakeOpen, setIntakeOpen] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [historyTab, setHistoryTab] = useState<'history' | 'diff' | 'commits'>('history');
  const [selectionMode, setSelectionMode] = useState(false);
  const [selected, setSelected] = useState(new Set<string>());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  const [deletingProject, setDeletingProject] = useState(false);
  const [deleteProjectError, setDeleteProjectError] = useState<string | null>(null);

  const project = snapshot?.projects.find(p => p.id === projectId);
  const allTasks = snapshot?.tasks ?? [];
  const workers = snapshot?.workers ?? [];

  const projectTasks = allTasks.filter(t => t.repo === projectId);
  const hierarchy = buildHierarchy(projectTasks);

  // Stats
  const running = projectTasks.filter(t => t.status === 'running').length;
  const planned = projectTasks.filter(t => t.status === 'planned' || t.status === 'ready').length;
  const done = projectTasks.filter(t => t.status === 'done').length;
  const blocked = projectTasks.filter(t => t.status === 'blocked').length;
  const activeAgents = workers.filter(w =>
    w.current_task_id && projectTasks.some(t => t.id === w.current_task_id),
  ).length;

  const selectedTask = selectedTaskId ? allTasks.find(t => t.id === selectedTaskId) : null;
  const isRunning = selectedTask?.status === 'running';

  // Task history query — polls every 3s while the agent is actively working
  const historyQuery = useQuery({
    queryKey: ['task-history', selectedTaskId],
    enabled: !!selectedTaskId,
    queryFn: () =>
      fetchTaskApiJson(`/api/tasks/${encodeURIComponent(selectedTaskId!)}/history`),
    refetchInterval: isRunning ? 3_000 : 10_000,
    retry: 2,
  });

  const diffQuery = useQuery({
    queryKey: ['task-diff', selectedTaskId],
    enabled: !!selectedTaskId && !!selectedTask?.branch,
    queryFn: () =>
      fetchTaskApiJson(`/api/tasks/${encodeURIComponent(selectedTaskId!)}/diff`),
    refetchInterval: 10_000,
    retry: 2,
  });

  const commitsQuery = useQuery({
    queryKey: ['task-commits', selectedTaskId],
    enabled: !!selectedTaskId && !!selectedTask?.branch,
    queryFn: () =>
      fetchTaskApiJson(`/api/tasks/${encodeURIComponent(selectedTaskId!)}/commits`),
    refetchInterval: 10_000,
    retry: 2,
  });

  // Unblock a task
  const qc = useQueryClient();
  async function unblockTask(taskId: string) {
    await fetch(`/api/tasks/${taskId}/transition`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: 'ready' }),
    });
    qc.invalidateQueries({ queryKey: ['snapshot'] });
  }

  function toggleSelect(node: TaskNode) {
    const ids = collectIds(node);
    const allIn = ids.every(id => selected.has(id));
    setSelected(prev => {
      const next = new Set(prev);
      if (allIn) ids.forEach(id => next.delete(id));
      else ids.forEach(id => next.add(id));
      return next;
    });
  }

  async function bulkAction(action: 'archive' | 'delete') {
    if (selected.size === 0) return;
    await fetch('/api/tasks/bulk-update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...selected], action, repo: projectId }),
    });
    setSelected(new Set());
    setSelectionMode(false);
    setConfirmDelete(false);
    qc.invalidateQueries({ queryKey: ['snapshot'] });
  }

  async function deleteProject() {
    setDeletingProject(true);
    setDeleteProjectError(null);
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(projectId)}/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: true }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data?.error ?? 'Failed to delete project');

      qc.invalidateQueries({ queryKey: ['snapshot'] });
      setConfirmDeleteProject(false);
      navigate('/projects');
    } catch (e) {
      setDeleteProjectError(e instanceof Error ? e.message : 'Failed to delete project');
    } finally {
      setDeletingProject(false);
    }
  }

  function exitSelection() {
    setSelectionMode(false);
    setSelected(new Set());
    setConfirmDelete(false);
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 gap-3 text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin" />
        <span className="text-sm">Loading…</span>
      </div>
    );
  }

  if (error || (!isLoading && !project)) {
    return (
      <div className="p-8 text-center">
        <AlertCircle className="w-8 h-8 mx-auto mb-3 text-destructive opacity-50" />
        <p className="text-muted-foreground text-sm mb-3">
          {error ? 'Failed to connect to backend.' : `Project "${projectId}" not found.`}
        </p>
        <button onClick={() => navigate('/projects')} className="text-primary text-sm">← Back to projects</button>
      </div>
    );
  }

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-5">

      {/* ── Header ── */}
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <button
          onClick={() => navigate('/projects')}
          className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground mb-4 transition-colors"
        >
          <ArrowLeft className="w-3.5 h-3.5" /> Back to Projects
        </button>

        <div className="glass-card p-5">
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              {/* Title row */}
              <div className="flex items-center gap-3 flex-wrap mb-1">
                <h1 className="text-xl font-bold tracking-tight text-foreground">{project!.name}</h1>
                <button
                  onClick={() => setExplorerOpen(true)}
                  className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-lg bg-sky-500/15 border border-sky-500/30 text-sky-400 hover:bg-sky-500/25 hover:border-sky-500/50 transition-all font-medium"
                >
                  <FolderOpen className="w-3.5 h-3.5" />
                  Browse Code
                </button>
              </div>

              {/* Git info */}
              <GitInfoStrip projectId={projectId} />

              {project!.repoUrl && (
                <p className="text-[10px] text-muted-foreground/40 mt-1 truncate">{project!.repoUrl}</p>
              )}
            </div>

            {/* Right side: agent controls + intake button */}
            <div className="flex flex-col items-end gap-3 shrink-0">
              <AgentControls />
              <button
                onClick={() => setIntakeOpen(true)}
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary/15 border border-primary/20 text-primary text-sm font-medium hover:bg-primary/25 transition-all"
              >
                <Plus className="w-4 h-4" />
                New Work
              </button>
              <button
                onClick={() => {
                  setDeleteProjectError(null);
                  setConfirmDeleteProject(true);
                }}
                disabled={deletingProject}
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-destructive/15 border border-destructive/20 text-destructive text-sm font-medium hover:bg-destructive/25 transition-all disabled:opacity-50"
                title="Delete the entire project (repo directory + related ES records)"
              >
                <Trash2 className="w-4 h-4" />
                Delete Project
              </button>
            </div>
          </div>
        </div>
      </motion.div>

      {/* ── Delete project confirmation ──────────────────────────────── */}
      <Dialog open={confirmDeleteProject} onOpenChange={v => setConfirmDeleteProject(v)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete project?</DialogTitle>
            <DialogDescription>
              This will remove the project directory from the workspace and delete related records from Elasticsearch (best-effort).
            </DialogDescription>
          </DialogHeader>
          {deleteProjectError && (
            <div className="mt-3 text-[12px] text-destructive">
              {deleteProjectError}
            </div>
          )}
          <div className="flex gap-2 justify-end mt-5">
            <button
              onClick={() => setConfirmDeleteProject(false)}
              disabled={deletingProject}
              className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              onClick={() => deleteProject()}
              disabled={deletingProject}
              className="px-3 py-2 rounded-lg bg-destructive/20 border border-destructive/30 text-destructive text-sm font-medium hover:bg-destructive/25 transition-colors disabled:opacity-50"
            >
              {deletingProject ? 'Deleting...' : 'Confirm Delete'}
            </button>
          </div>
        </DialogContent>
      </Dialog>

      {/* ── Stats ── */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[
          { label: 'Running', value: running, cls: 'text-primary' },
          { label: 'Planned', value: planned, cls: 'text-muted-foreground' },
          { label: 'Done', value: done, cls: 'text-success' },
          { label: 'Blocked', value: blocked, cls: 'text-destructive' },
          { label: 'Agents', value: activeAgents, cls: 'text-foreground' },
        ].map(s => (
          <div key={s.label} className="glass-card p-4 text-center">
            <div className={`text-xl font-bold ${s.cls}`}>{s.value}</div>
            <div className="text-[10px] text-muted-foreground mt-0.5">{s.label}</div>
          </div>
        ))}
      </div>

      {/* ── Branch management ── */}
      <div className="flex items-center justify-end">
        <LocalBranchesDialog projectId={projectId} currentBranch={currentBranch} />
      </div>

      {/* ── Work hierarchy + task detail ── */}
      <div className="grid grid-cols-1 xl:grid-cols-[1fr_380px] gap-4">
        {/* Hierarchy tree */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-foreground">Work Hierarchy</h2>
            <div className="flex items-center gap-2">
              <button
                onClick={() => selectionMode ? exitSelection() : setSelectionMode(true)}
                className={`flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md border transition-all ${
                  selectionMode
                    ? 'bg-primary/15 border-primary/30 text-primary'
                    : 'bg-white/5 border-white/10 text-muted-foreground hover:text-primary hover:border-primary/30'
                }`}
              >
                <CheckSquare className="w-3 h-3" />
                {selectionMode ? `Cancel${selected.size > 0 ? ` (${selected.size})` : ''}` : 'Select'}
              </button>
              {!selectionMode && (
                <button
                  onClick={() => setIntakeOpen(true)}
                  className="flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-md bg-white/5 border border-white/10 text-muted-foreground hover:text-primary hover:border-primary/30 transition-all"
                >
                  <Plus className="w-3 h-3" /> Add Work
                </button>
              )}
            </div>
          </div>
          {projectTasks.length === 0 ? (
            <div className="glass-card p-10 text-center">
              <Unlink className="w-8 h-8 mx-auto mb-3 opacity-20" />
              <p className="text-sm text-muted-foreground mb-3">No tasks yet for this project.</p>
              <button
                onClick={() => setIntakeOpen(true)}
                className="flex items-center gap-1.5 mx-auto text-xs px-4 py-2 rounded-lg bg-primary/15 border border-primary/20 text-primary hover:bg-primary/25 transition-all"
              >
                <Plus className="w-3.5 h-3.5" /> Plan New Work
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              {hierarchy.map(node => (
                <HierarchyNode
                  key={node._id}
                  node={node}
                  depth={0}
                  selectedTaskId={selectedTaskId}
                  onSelect={setSelectedTaskId}
                  onUnblock={unblockTask}
                  selectionMode={selectionMode}
                  selected={selected}
                  onToggleSelect={toggleSelect}
                />
              ))}
            </div>
          )}

          {/* ── Bulk action bar ── */}
          {selected.size > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className="sticky bottom-0 mt-3 flex items-center gap-2 p-3 glass-card border border-primary/20 bg-background/95 backdrop-blur"
            >
              <span className="text-xs text-muted-foreground flex-1">
                {selected.size} item{selected.size !== 1 ? 's' : ''} selected
              </span>
              <button
                onClick={() => bulkAction('archive')}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 hover:bg-amber-500/20 transition-all"
              >
                <Archive className="w-3.5 h-3.5" /> Archive
              </button>
              {confirmDelete ? (
                <>
                  <span className="text-xs text-destructive font-medium">Delete {selected.size} items?</span>
                  <button
                    onClick={() => bulkAction('delete')}
                    className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-destructive/20 border border-destructive/40 text-destructive hover:bg-destructive/30 transition-all font-semibold"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Confirm Delete
                  </button>
                  <button
                    onClick={() => setConfirmDelete(false)}
                    className="text-xs px-2 py-1.5 text-muted-foreground hover:text-foreground transition-colors"
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  onClick={() => setConfirmDelete(true)}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive hover:bg-destructive/20 transition-all"
                >
                  <Trash2 className="w-3.5 h-3.5" /> Delete
                </button>
              )}
              <button
                onClick={() => { setSelected(new Set()); setConfirmDelete(false); }}
                className="p-1.5 text-muted-foreground/50 hover:text-muted-foreground rounded transition-colors"
                title="Clear selection"
              >
                <XIcon className="w-3.5 h-3.5" />
              </button>
            </motion.div>
          )}
        </div>

        {/* Task detail panel */}
        {selectedTask && (
          <div className="glass-card overflow-hidden flex flex-col max-h-[700px]">
            {/* Task header */}
            <div className="p-4 border-b border-border shrink-0">
              <div className="flex items-start gap-2 mb-2">
                <ItemTypeBadge type={selectedTask.item_type} />
                <StatusBadge status={selectedTask.status} pulse />
                {selectedTask.pr_status === 'failed' && (
                  <span className="text-[10px] text-destructive flex items-center gap-1">
                    <AlertCircle className="w-3 h-3" /> PR failed
                  </span>
                )}
              </div>
              <h3 className="text-sm font-semibold text-foreground mb-1">{selectedTask.title}</h3>
              {selectedTask.objective && (
                <p className="text-xs text-muted-foreground">{selectedTask.objective}</p>
              )}
            </div>

            {/* Git info row */}
            {(selectedTask.branch || selectedTask.pr_url) && (
              <div className="px-4 py-2 border-b border-border bg-muted/5 space-y-1 shrink-0">
                {selectedTask.branch && (
                  <div className="flex items-center gap-2 text-xs">
                    <GitBranch className="w-3.5 h-3.5 text-primary" />
                    <code className="text-primary font-mono">{selectedTask.branch}</code>
                    {selectedTask.target_branch && (
                      <span className="text-muted-foreground/50">→ {selectedTask.target_branch}</span>
                    )}
                  </div>
                )}
                {selectedTask.commit_sha && (
                  <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                    <GitCommit className="w-3 h-3" />
                    <code className="font-mono">{selectedTask.commit_sha.slice(0, 7)}</code>
                    {selectedTask.commit_message && (
                      <span className="truncate">{selectedTask.commit_message}</span>
                    )}
                  </div>
                )}
                {selectedTask.pr_url && (
                  <div className="flex items-center gap-2 text-xs">
                    <GitPullRequest className="w-3.5 h-3.5 text-success" />
                    <a href={selectedTask.pr_url} target="_blank" rel="noopener noreferrer"
                      className="text-success hover:underline flex items-center gap-1">
                      View PR <ExternalLink className="w-3 h-3" />
                    </a>
                  </div>
                )}
                {selectedTask.pr_status === 'failed' && selectedTask.pr_error && (
                  <p className="text-[10px] text-destructive break-words" title={selectedTask.pr_error}>
                    {selectedTask.pr_error}
                  </p>
                )}
              </div>
            )}

            {/* History / Diff / Commits tabs */}
            {selectedTask.branch && (
              <div className="flex border-b border-border shrink-0">
                {(['history', 'diff', 'commits'] as const).map(tab => (
                  <button key={tab} onClick={() => setHistoryTab(tab)}
                    className={`flex-1 text-[11px] py-2 capitalize transition-colors ${
                      historyTab === tab
                        ? 'text-primary border-b-2 border-primary -mb-px bg-primary/5'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {tab}
                  </button>
                ))}
              </div>
            )}

            {/* Tab content */}
            <div className="flex-1 overflow-y-auto p-4 space-y-2 text-xs">

              {/* History tab */}
              {historyTab === 'history' && (
                <>
                  {/* Live working indicator */}
                  {isRunning && (
                    <div className="flex items-center gap-2 mb-3 px-2 py-1.5 rounded-lg bg-primary/5 border border-primary/15">
                      <Loader2 className="w-3 h-3 animate-spin text-primary shrink-0" />
                      <span className="text-[11px] text-primary font-medium">Agent is working…</span>
                      <span className="text-[10px] text-muted-foreground ml-auto">updates every 3s</span>
                    </div>
                  )}

                  {historyQuery.isLoading && <div className="flex items-center gap-2 text-muted-foreground"><Loader2 className="w-3.5 h-3.5 animate-spin" />Loading…</div>}
                  {historyQuery.isError && (
                    <div className="flex items-start gap-2 text-destructive text-xs">
                      <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                      <span>{historyQuery.error instanceof Error ? historyQuery.error.message : 'Failed to load history'}</span>
                    </div>
                  )}
                  {historyQuery.data?.history?.length
                    ? historyQuery.data.history.map((e: { ts: string; role: string; summary: string; type?: string }, i: number) => {
                        const isAgentNote = e.type === 'agent_note';
                        return (
                          <div key={i} className={`border-l-2 pl-3 py-1 ${isAgentNote ? 'border-primary/40' : 'border-muted/30'}`}>
                            <div className="flex items-center gap-2 text-[10px] text-muted-foreground mb-0.5">
                              {isAgentNote
                                ? <span className="flex items-center gap-1 text-primary/70 font-medium"><span className="w-1.5 h-1.5 rounded-full bg-primary/60 inline-block" />agent</span>
                                : <span className="text-foreground/60 font-medium">{e.role}</span>
                              }
                              {e.ts && <span>{timeAgo(e.ts)}</span>}
                            </div>
                            <p className={`${isAgentNote ? 'text-foreground/90 font-mono text-[10px]' : 'text-foreground/80'}`}>{e.summary}</p>
                          </div>
                        );
                      })
                    : !historyQuery.isLoading && !historyQuery.isError && <p className="text-muted-foreground">No history yet.</p>}

                  {/* Acceptance criteria */}
                  {selectedTask.acceptance_criteria && selectedTask.acceptance_criteria.length > 0 && (
                    <div className="mt-3 pt-3 border-t border-border">
                      <p className="text-[10px] font-semibold text-muted-foreground mb-2">ACCEPTANCE CRITERIA</p>
                      <ul className="space-y-1">
                        {selectedTask.acceptance_criteria.map((ac, i) => (
                          <li key={i} className="flex items-start gap-2 text-foreground/70">
                            <span className="text-success mt-0.5 shrink-0">✓</span>{ac}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </>
              )}

              {/* Diff tab */}
              {historyTab === 'diff' && (
                <>
                  {diffQuery.isLoading && <div className="flex items-center gap-2 text-muted-foreground"><Loader2 className="w-3.5 h-3.5 animate-spin" />Loading diff…</div>}
                  {diffQuery.isError && (
                    <div className="flex items-start gap-2 text-destructive text-xs mb-2">
                      <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                      <span>{diffQuery.error instanceof Error ? diffQuery.error.message : 'Failed to load diff'}</span>
                    </div>
                  )}
                  {diffQuery.data?.files?.length > 0 && (
                    <div className="space-y-1 mb-3">
                      {diffQuery.data.files.map((f: { path: string; insertions: number; deletions: number; status: string }) => (
                        <div key={f.path} className="flex items-center gap-2 text-[10px]">
                          <span className={`px-1.5 py-0.5 rounded font-medium ${f.status === 'added' ? 'bg-success/10 text-success' : f.status === 'deleted' ? 'bg-destructive/10 text-destructive' : 'bg-primary/10 text-primary'}`}>{f.status}</span>
                          <code className="text-foreground/80 truncate flex-1">{f.path}</code>
                          <span className="text-success shrink-0">+{f.insertions}</span>
                          <span className="text-destructive shrink-0">-{f.deletions}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  {diffQuery.data?.diff ? (
                    <pre className="text-[10px] font-mono overflow-x-auto bg-muted/10 rounded p-2">
                      {diffQuery.data.diff.split('\n').map((line: string, i: number) => (
                        <div key={i} className={line.startsWith('+') && !line.startsWith('+++') ? 'text-success' : line.startsWith('-') && !line.startsWith('---') ? 'text-destructive' : line.startsWith('@@') ? 'text-primary/70' : 'text-foreground/60'}>{line}</div>
                      ))}
                    </pre>
                  ) : !diffQuery.isLoading && !diffQuery.isError && <p className="text-muted-foreground">No changes vs default branch.</p>}
                </>
              )}

              {/* Commits tab */}
              {historyTab === 'commits' && (
                <>
                  {commitsQuery.isLoading && <div className="flex items-center gap-2 text-muted-foreground"><Loader2 className="w-3.5 h-3.5 animate-spin" />Loading commits…</div>}
                  {commitsQuery.isError && (
                    <div className="flex items-start gap-2 text-destructive text-xs mb-2">
                      <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                      <span>{commitsQuery.error instanceof Error ? commitsQuery.error.message : 'Failed to load commits'}</span>
                    </div>
                  )}
                  {commitsQuery.data?.commits?.length > 0
                    ? commitsQuery.data.commits.map((c: { sha: string; message: string; author: string; date: string }) => (
                        <div key={c.sha} className="border-l-2 border-muted/30 pl-3 py-1">
                          <div className="flex items-center gap-2 mb-0.5">
                            <code className="text-[10px] text-muted-foreground font-mono">{c.sha.slice(0, 7)}</code>
                            <span className="text-[10px] text-muted-foreground">{timeAgo(c.date)}</span>
                          </div>
                          <p className="text-foreground/80 text-[11px]">{c.message}</p>
                          <p className="text-[10px] text-muted-foreground">{c.author}</p>
                        </div>
                      ))
                    : !commitsQuery.isLoading && !commitsQuery.isError && <p className="text-muted-foreground">No commits ahead of default branch.</p>}
                </>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Modals */}
      <FileExplorerModal
        open={explorerOpen}
        onOpenChange={setExplorerOpen}
        projectName={project!.name}
        projectId={projectId}
      />
      <IntakeModal
        open={intakeOpen}
        onOpenChange={setIntakeOpen}
        projectId={projectId}
        projectName={project!.name}
      />
    </div>
  );
}

// ─── Helper components ────────────────────────────────────────────────────────

function ItemTypeBadge({ type }: { type?: string }) {
  const cfg: Record<string, { label: string; cls: string }> = {
    epic:    { label: 'EPIC',    cls: 'bg-primary/10 text-primary' },
    feature: { label: 'FEATURE', cls: 'bg-purple-500/10 text-purple-400' },
    story:   { label: 'STORY',   cls: 'bg-cyan-500/10 text-cyan-400' },
    task:    { label: 'TASK',    cls: 'bg-muted text-muted-foreground' },
  };
  const c = cfg[type ?? 'task'] ?? cfg.task;
  return <span className={`text-[10px] font-medium px-2 py-0.5 rounded ${c.cls}`}>{c.label}</span>;
}

function HierarchyNode({
  node, depth, selectedTaskId, onSelect, onUnblock,
  selectionMode, selected, onToggleSelect,
}: {
  node: TaskNode;
  depth: number;
  selectedTaskId: string | null;
  onSelect: (id: string) => void;
  onUnblock: (id: string) => void;
  selectionMode: boolean;
  selected: Set<string>;
  onToggleSelect: (node: TaskNode) => void;
}) {
  const [open, setOpen] = useState(depth < 2);
  const hasChildren = node.children.length > 0;
  const isSelected = selectedTaskId === node.id;
  const isBlocked = node.status === 'blocked';

  const allIds = collectIds(node);
  const checkedCount = allIds.filter(id => selected.has(id)).length;
  const isChecked = checkedCount === allIds.length && allIds.length > 0;
  const isIndeterminate = checkedCount > 0 && !isChecked;

  return (
    <div className={`glass-card overflow-hidden ${depth > 0 ? 'ml-5 border-l-2 border-primary/10' : ''} ${selectionMode && isChecked ? 'ring-1 ring-primary/20' : ''}`}>
      <button
        onClick={() => {
          if (selectionMode) {
            onToggleSelect(node);
          } else {
            if (hasChildren && !open) setOpen(true);
            onSelect(node.id);
          }
        }}
        className={`w-full flex items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-muted/20 ${isSelected && !selectionMode ? 'bg-primary/5' : ''} ${selectionMode && isChecked ? 'bg-primary/5' : ''}`}
      >
        {/* Left control: checkbox in selection mode, chevron toggle otherwise */}
        {selectionMode ? (
          <div
            className={`w-4 h-4 rounded border-2 flex items-center justify-center shrink-0 transition-all ${
              isChecked
                ? 'bg-primary border-primary'
                : isIndeterminate
                ? 'bg-primary/30 border-primary/60'
                : 'border-muted-foreground/40 hover:border-primary/60'
            }`}
          >
            {isChecked && <span className="text-[8px] text-white font-bold leading-none">✓</span>}
            {isIndeterminate && <span className="text-[8px] text-primary font-bold leading-none">−</span>}
          </div>
        ) : (
          hasChildren
            ? (
              <span
                role="button"
                onClick={e => { e.stopPropagation(); setOpen(o => !o); }}
                className="shrink-0 p-0.5 -m-0.5 rounded hover:bg-muted/40 transition-colors"
              >
                {open
                  ? <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />
                  : <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />}
              </span>
            )
            : <span className="w-3.5 h-3.5 shrink-0" />
        )}

        <ItemTypeBadge type={node.item_type} />
        <span className="text-[10px] font-mono text-muted-foreground/50 shrink-0">#{node.id}</span>
        <span className="text-xs font-medium text-foreground flex-1 truncate">{node.title}</span>
        <div className="flex items-center gap-2 shrink-0">
          <StatusBadge status={node.status} pulse />
          {node.branch && <GitBranch className="w-3 h-3 text-primary/60" />}
          {node.pr_status === 'failed' && <AlertCircle className="w-3 h-3 text-destructive" />}
          {isBlocked && !selectionMode && (
            <button
              onClick={e => { e.stopPropagation(); onUnblock(node.id); }}
              className="text-[10px] px-2 py-0.5 rounded bg-warning/10 border border-warning/20 text-warning hover:bg-warning/20 transition-all"
            >
              Unblock
            </button>
          )}
          {/* Expander button in selection mode for nodes with children */}
          {selectionMode && hasChildren && (
            <button
              onClick={e => { e.stopPropagation(); setOpen(!open); }}
              className="p-0.5 text-muted-foreground/40 hover:text-muted-foreground rounded"
            >
              {open
                ? <ChevronDown className="w-3.5 h-3.5" />
                : <ChevronRight className="w-3.5 h-3.5" />}
            </button>
          )}
        </div>
      </button>

      {open && hasChildren && (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="border-t border-border/50">
          {node.children.map(child => (
            <HierarchyNode
              key={child._id}
              node={child}
              depth={depth + 1}
              selectedTaskId={selectedTaskId}
              onSelect={onSelect}
              onUnblock={onUnblock}
              selectionMode={selectionMode}
              selected={selected}
              onToggleSelect={onToggleSelect}
            />
          ))}
        </motion.div>
      )}
    </div>
  );
}
