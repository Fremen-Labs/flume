import { useState } from 'react';
import { motion } from 'framer-motion';
import { useNavigate } from 'react-router-dom';
import { Search, FolderOpen, Plus, Loader2, AlertCircle } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { useSnapshot } from '@/hooks/useSnapshot';
import { FileExplorerModal } from '@/components/FileExplorerModal';
import type { ApiProject } from '@/types';
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
      planned: ptasks.filter(t => t.status === 'planned' || t.status === 'ready').length,
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

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error ?? data.detail ?? 'Failed to create project');
      }

      setCreateOpen(false);
      setCreateName('');
      setCreateRepoUrl('');
      qc.invalidateQueries({ queryKey: ['snapshot'] });
    } catch (e: unknown) {
      setCreateError(e instanceof Error ? e.message : 'Failed to create project');
    } finally {
      setCreateLoading(false);
    }
  }

  return (
    <div className="p-6 lg:p-8 max-w-[1600px] mx-auto space-y-6 relative">

      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="relative z-10">
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Projects</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {isLoading ? 'Loading…' : `${projects.length} project${projects.length !== 1 ? 's' : ''} · AI-driven software delivery`}
        </p>
      </motion.div>

      {/* Toolbar */}
      <div className="flex flex-col sm:flex-row gap-3 relative z-10">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search projects…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2.5 text-sm rounded-lg glass-surface text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </div>
        <div className="flex items-start sm:items-center sm:justify-end">
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-primary/15 border border-primary/20 text-primary text-sm font-medium hover:bg-primary/25 hover:border-primary/30 transition-colors"
          >
            <Plus className="w-4 h-4" />
            New Project
          </button>
        </div>
      </div>

      {/* Loading */}
      {isLoading && (
        <div className="flex items-center justify-center py-20 gap-3 text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin" />
          <span className="text-sm">Loading projects…</span>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="flex items-center gap-3 p-4 rounded-lg bg-destructive/10 border border-destructive/20 text-destructive text-sm">
          <AlertCircle className="w-4 h-4 shrink-0" />
          Failed to connect to backend. Is server.py running on port 8765?
        </div>
      )}

      {/* Project Grid */}
      {!isLoading && !error && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4 relative z-10">
          {filtered.map((project, i) => {
            const stats = projectStats(project.id);
            return (
              <motion.div
                key={project.id}
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05 }}
                whileHover={{ y: -4, transition: { duration: 0.25 } }}
                onClick={() => navigate(`/projects/${encodeURIComponent(project.id)}`)}
                className="glass-card-glow cursor-pointer group relative overflow-hidden hover-lift"
              >
                {/* Background */}
                <div className="absolute inset-0">
                  <img
                    src={projectBgs[i % projectBgs.length]}
                    alt=""
                    className="w-full h-full object-cover opacity-10 group-hover:opacity-20 transition-opacity duration-700"
                  />
                  <div className="absolute inset-0 bg-gradient-to-t from-card via-card/90 to-card/40" />
                </div>
                <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none z-[2]"
                  style={{ background: 'linear-gradient(135deg, rgba(255,255,255,0.04) 0%, transparent 50%, rgba(255,255,255,0.02) 100%)' }} />

                <div className="relative p-5 z-[3]">
                  <div className="flex items-start justify-between mb-3">
                    <div className="flex-1 min-w-0">
                      <h3 className="text-sm font-semibold text-foreground truncate group-hover:text-primary transition-colors mb-1">
                        {project.name}
                      </h3>
                      <div className="flex items-center gap-2 flex-wrap">
                        {project.repoUrl && (
                          <span className="text-[10px] text-muted-foreground/60 truncate max-w-[180px]">{project.repoUrl}</span>
                        )}
                      </div>
                    </div>
                    <div className="text-right shrink-0 ml-3">
                      <div className="text-lg font-bold text-foreground">{stats.total}</div>
                      <div className="text-[10px] text-muted-foreground">tasks</div>
                    </div>
                  </div>

                  {/* Stats bar */}
                  <div className="grid grid-cols-4 gap-2 p-3 rounded-lg glass-surface mb-3">
                    <div className="text-center">
                      <div className="text-sm font-semibold text-primary">{stats.running}</div>
                      <div className="text-[10px] text-muted-foreground">Running</div>
                    </div>
                    <div className="text-center">
                      <div className="text-sm font-semibold text-muted-foreground">{stats.planned}</div>
                      <div className="text-[10px] text-muted-foreground">Planned</div>
                    </div>
                    <div className="text-center">
                      <div className="text-sm font-semibold text-success">{stats.done}</div>
                      <div className="text-[10px] text-muted-foreground">Done</div>
                    </div>
                    <div className="text-center">
                      <div className="text-sm font-semibold text-destructive">{stats.blocked}</div>
                      <div className="text-[10px] text-muted-foreground">Blocked</div>
                    </div>
                  </div>

                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>{stats.agents > 0 ? `${stats.agents} agent${stats.agents !== 1 ? 's' : ''} active` : 'No active agents'}</span>
                    <div className="flex items-center gap-2">
                        <button
                          onClick={e => { e.stopPropagation(); setExplorerProject(project); }}
                          className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded-md bg-sky-500/15 border border-sky-500/30 text-sky-400 hover:bg-sky-500/25 hover:border-sky-500/50 transition-all font-medium"
                        >
                          <FolderOpen className="w-3 h-3" />
                          Browse
                        </button>
                      <span className="text-[10px] text-muted-foreground/50">{timeAgo(project.created_at)}</span>
                    </div>
                  </div>
                </div>
              </motion.div>
            );
          })}

          {/* Empty state */}
          {filtered.length === 0 && !isLoading && (
            <div className="col-span-full glass-card p-12 text-center text-sm text-muted-foreground">
              <Plus className="w-8 h-8 mx-auto mb-3 opacity-30" />
              <p>
                {search ? `No projects match "${search}"` : 'No projects yet. Create one with the “New Project” dialog.'}
              </p>
            </div>
          )}
        </div>
      )}

      {explorerProject && (
        <FileExplorerModal
          open={!!explorerProject}
          onOpenChange={open => { if (!open) setExplorerProject(null); }}
          projectName={explorerProject.name}
          projectId={explorerProject.id}
        />
      )}

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
        <DialogContent className="bg-[#0a0f1a] border-white/10 text-foreground">
          <DialogHeader>
            <DialogTitle>Create Project</DialogTitle>
            <DialogDescription>
              Optionally provide a Git repo URL to clone into the workspace (for Elasticsearch indexing by agents).
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <div className="text-xs text-muted-foreground">Project name</div>
              <Input
                value={createName}
                onChange={e => setCreateName(e.target.value)}
                placeholder="e.g. customer-onboarding"
                autoFocus
              />
            </div>

            <div className="space-y-1">
              <div className="text-xs text-muted-foreground">Repo URL (optional)</div>
              <Input
                value={createRepoUrl}
                onChange={e => setCreateRepoUrl(e.target.value)}
                placeholder="https://github.com/org/repo.git"
                type="url"
              />
            </div>

            {createError && (
              <div className="flex items-start gap-2 text-destructive text-xs">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
                <span>{createError}</span>
              </div>
            )}
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <button
              onClick={() => setCreateOpen(false)}
              className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors"
              disabled={createLoading}
            >
              Cancel
            </button>
            <button
              onClick={createProject}
              disabled={createLoading}
              className="flex items-center gap-2 px-4 py-2 rounded-lg bg-success/15 border border-success/20 text-success text-sm font-medium hover:bg-success/25 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {createLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              {createLoading ? 'Creating…' : 'Create'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
