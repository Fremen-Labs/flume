import { useState, useEffect, useCallback, useRef } from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { PanelGroup, Panel, PanelResizeHandle } from 'react-resizable-panels';
import {
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
  GitBranch,
  GitCompare,
  GitCompareArrows,
  Loader2,
  AlertCircle,
  X,
  Search,
  FileCode,
  FileJson,
  FileText,
  File,
  ChevronsUpDown,
  Check,
  ArrowRight,
  CheckCircle2,
} from 'lucide-react';
import hljs from 'highlight.js';
import 'highlight.js/styles/github-dark.css';
import { cn } from '@/lib/utils';

// ─── Types ────────────────────────────────────────────────────────────────────

interface TreeEntry {
  path: string;
  type: 'tree' | 'blob';
  size: string;
}

interface TreeNode {
  name: string;
  path: string;
  type: 'tree' | 'blob';
  children?: TreeNode[];
}

interface DiffFile {
  path: string;
  insertions: number;
  deletions: number;
  status: 'added' | 'deleted' | 'modified' | 'renamed';
}

interface DiffResult {
  base: string;
  head: string;
  files: DiffFile[];
  diff: string;
  truncated: boolean;
  identical: boolean;
  error?: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function buildTree(entries: TreeEntry[]): TreeNode[] {
  const sorted = [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'tree' ? -1 : 1;
    return a.path.localeCompare(b.path);
  });

  const root: TreeNode[] = [];
  const map = new Map<string, TreeNode>();

  for (const entry of sorted) {
    const parts = entry.path.split('/');
    const name = parts[parts.length - 1];
    const parentPath = parts.slice(0, -1).join('/');
    const node: TreeNode = {
      name,
      path: entry.path,
      type: entry.type,
      children: entry.type === 'tree' ? [] : undefined,
    };
    map.set(entry.path, node);
    if (!parentPath) {
      root.push(node);
    } else {
      const parent = map.get(parentPath);
      if (parent?.children) {
        parent.children.push(node);
      }
    }
  }

  function sortChildren(nodes: TreeNode[]): TreeNode[] {
    return nodes
      .sort((a, b) => {
        if (a.type !== b.type) return a.type === 'tree' ? -1 : 1;
        return a.name.localeCompare(b.name);
      })
      .map(n => n.children ? { ...n, children: sortChildren(n.children) } : n);
  }

  return sortChildren(root);
}

function getLanguage(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  const name = filename.toLowerCase();
  if (name === 'dockerfile') return 'dockerfile';
  if (name === 'makefile') return 'makefile';
  const map: Record<string, string> = {
    ts: 'typescript', tsx: 'typescript',
    js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
    py: 'python', go: 'go', java: 'java', rs: 'rust', rb: 'ruby',
    json: 'json', yaml: 'yaml', yml: 'yaml',
    md: 'markdown', mdx: 'markdown',
    css: 'css', scss: 'scss', sass: 'scss', less: 'less',
    html: 'html', htm: 'html', xml: 'xml',
    sh: 'bash', bash: 'bash', zsh: 'bash',
    sql: 'sql', toml: 'toml',
    c: 'c', cpp: 'cpp', cc: 'cpp', h: 'c', hpp: 'cpp',
    php: 'php', swift: 'swift', kt: 'kotlin', dart: 'dart',
    lock: 'plaintext', env: 'bash',
  };
  return map[ext] ?? 'plaintext';
}

function getFileColor(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  if (['ts', 'tsx'].includes(ext)) return 'text-blue-400';
  if (['js', 'jsx', 'mjs'].includes(ext)) return 'text-yellow-400';
  if (['json'].includes(ext)) return 'text-orange-400';
  if (['py'].includes(ext)) return 'text-green-400';
  if (['go'].includes(ext)) return 'text-cyan-400';
  if (['rs'].includes(ext)) return 'text-orange-500';
  if (['css', 'scss', 'sass', 'less'].includes(ext)) return 'text-purple-400';
  if (['html', 'htm'].includes(ext)) return 'text-red-400';
  if (['md', 'mdx'].includes(ext)) return 'text-gray-300';
  if (['sh', 'bash', 'zsh'].includes(ext)) return 'text-emerald-400';
  if (['yml', 'yaml', 'toml'].includes(ext)) return 'text-pink-400';
  if (['java', 'kt'].includes(ext)) return 'text-orange-400';
  return 'text-muted-foreground';
}

function FileIcon({ filename, className }: { filename: string; className?: string }) {
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  const color = getFileColor(filename);
  if (['ts', 'tsx', 'js', 'jsx', 'py', 'go', 'rs', 'java', 'kt', 'swift', 'dart', 'rb', 'php', 'c', 'cpp', 'h'].includes(ext)) {
    return <FileCode className={cn('w-3.5 h-3.5 shrink-0', color, className)} />;
  }
  if (['json', 'toml'].includes(ext)) {
    return <FileJson className={cn('w-3.5 h-3.5 shrink-0', color, className)} />;
  }
  if (['md', 'mdx', 'txt', 'rst'].includes(ext)) {
    return <FileText className={cn('w-3.5 h-3.5 shrink-0', color, className)} />;
  }
  return <File className={cn('w-3.5 h-3.5 shrink-0', color, className)} />;
}

function flattenTree(nodes: TreeNode[], query: string): TreeNode[] {
  const result: TreeNode[] = [];
  const q = query.toLowerCase();
  function walk(n: TreeNode) {
    if (n.type === 'blob' && n.name.toLowerCase().includes(q)) result.push(n);
    n.children?.forEach(walk);
  }
  nodes.forEach(walk);
  return result;
}

// ─── Branch Picker ────────────────────────────────────────────────────────────

interface BranchPickerProps {
  branches: string[];
  value: string;
  onChange: (b: string) => void;
  label?: string;
  disabled?: boolean;
}

function BranchPicker({ branches, value, onChange, label, disabled }: BranchPickerProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setSearch('');
      }
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const filtered = branches.filter(b => !search || b.toLowerCase().includes(search.toLowerCase()));

  return (
    <div ref={ref} className="relative shrink-0">
      {label && <span className="text-[10px] text-muted-foreground/50 mr-1.5">{label}</span>}
      <button
        onClick={() => { if (!disabled) { setOpen(o => !o); setSearch(''); } }}
        disabled={disabled}
        className={cn(
          'flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-lg bg-white/5 border border-white/10 text-muted-foreground hover:text-foreground hover:border-primary/30 transition-all max-w-[220px]',
          disabled && 'opacity-50 cursor-not-allowed',
        )}
      >
        <GitBranch className="w-3 h-3 text-primary/70 shrink-0" />
        <span className="truncate font-mono">{value || '…'}</span>
        {!disabled && <ChevronsUpDown className="w-3 h-3 shrink-0 opacity-50" />}
      </button>

      {open && (
        <div className="absolute top-full left-0 mt-1 z-50 w-64 rounded-lg bg-[#0d1117] border border-white/10 shadow-2xl overflow-hidden">
          <div className="p-2 border-b border-white/8">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-muted-foreground/50" />
              <input
                type="text"
                placeholder="Search branches…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                autoFocus
                className="w-full pl-7 pr-3 py-1.5 text-xs rounded-md bg-white/5 border border-white/8 text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:border-primary/40 transition-colors"
              />
            </div>
          </div>
          <div className="max-h-52 overflow-y-auto py-1">
            {filtered.map(b => (
              <button
                key={b}
                onClick={() => { onChange(b); setOpen(false); setSearch(''); }}
                className={cn(
                  'flex items-center gap-2 w-full text-left px-3 py-1.5 text-xs transition-colors',
                  b === value
                    ? 'text-primary bg-primary/10'
                    : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                )}
              >
                <GitBranch className="w-3 h-3 shrink-0 opacity-60" />
                <span className="truncate font-mono flex-1">{b}</span>
                {b === value && <Check className="w-3 h-3 shrink-0" />}
              </button>
            ))}
            {filtered.length === 0 && (
              <p className="px-3 py-2 text-xs text-muted-foreground/50">No branches match</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Diff Viewer ──────────────────────────────────────────────────────────────

function DiffViewer({ result, loading, error }: { result: DiffResult | null; loading: boolean; error: string }) {
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  useEffect(() => {
    setSelectedFile(null);
  }, [result]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground">
        <Loader2 className="w-6 h-6 animate-spin" />
        <span className="text-xs">Computing diff…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-destructive px-6 text-center">
        <AlertCircle className="w-6 h-6" />
        <span className="text-sm">{error}</span>
      </div>
    );
  }

    if (!result) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground select-none">
        <GitCompareArrows className="w-10 h-10 opacity-20" />
        <p className="text-sm opacity-50">Select branches above and click Compare</p>
      </div>
    );
  }

  if (result.identical) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 text-muted-foreground select-none">
        <div className="flex items-center justify-center w-16 h-16 rounded-full bg-emerald-500/10 border border-emerald-500/20">
          <CheckCircle2 className="w-8 h-8 text-emerald-400" />
        </div>
        <div className="text-center">
          <p className="text-sm font-medium text-foreground">Branches are identical</p>
          <p className="text-xs text-muted-foreground/60 mt-1">
            <span className="font-mono">{result.base}</span>
            {' '}and{' '}
            <span className="font-mono">{result.head}</span>
            {' '}have no differences.
          </p>
        </div>
      </div>
    );
  }

  // Filter diff lines to selected file if one is chosen
  const diffLines = result.diff.split('\n');
  let filteredLines: string[] = diffLines;
  if (selectedFile) {
    const start = diffLines.findIndex(l => l.startsWith('diff --git') && l.includes(selectedFile));
    if (start !== -1) {
      const end = diffLines.findIndex((l, i) => i > start && l.startsWith('diff --git'));
      filteredLines = end === -1 ? diffLines.slice(start) : diffLines.slice(start, end);
    }
  }

  const statusColor: Record<string, string> = {
    added: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/20',
    deleted: 'text-red-400 bg-red-500/10 border-red-500/20',
    modified: 'text-blue-400 bg-blue-500/10 border-blue-500/20',
    renamed: 'text-amber-400 bg-amber-500/10 border-amber-500/20',
  };
  const statusLabel: Record<string, string> = {
    added: 'A', deleted: 'D', modified: 'M', renamed: 'R',
  };

  const totalInsertions = result.files.reduce((s, f) => s + f.insertions, 0);
  const totalDeletions = result.files.reduce((s, f) => s + f.deletions, 0);

  return (
    <div className="flex h-full overflow-hidden">
      {/* File list sidebar */}
      <div className="w-64 shrink-0 border-r border-white/8 flex flex-col overflow-hidden">
        {/* Summary bar */}
        <div className="px-3 py-2 border-b border-white/8 bg-white/[0.02] shrink-0">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              {result.files.length} file{result.files.length !== 1 ? 's' : ''} changed
            </span>
            <div className="flex items-center gap-2 text-[11px]">
              <span className="text-emerald-400">+{totalInsertions}</span>
              <span className="text-red-400">-{totalDeletions}</span>
            </div>
          </div>
        </div>

        {/* File list */}
        <div className="flex-1 overflow-y-auto py-1 scrollbar-thin">
          <button
            onClick={() => setSelectedFile(null)}
            className={cn(
              'flex items-center gap-2 w-full text-left px-3 py-1.5 text-xs transition-colors',
              selectedFile === null
                ? 'bg-primary/10 text-primary'
                : 'text-muted-foreground hover:bg-white/5 hover:text-foreground',
            )}
          >
            <GitCompareArrows className="w-3.5 h-3.5 shrink-0 opacity-60" />
            <span className="truncate font-medium">All changes</span>
          </button>
          {result.files.map(f => (
            <button
              key={f.path}
              onClick={() => setSelectedFile(f.path)}
              className={cn(
                'flex items-center gap-2 w-full text-left px-3 py-1.5 text-xs transition-colors',
                selectedFile === f.path
                  ? 'bg-primary/10 text-primary'
                  : 'text-muted-foreground hover:bg-white/5 hover:text-foreground',
              )}
            >
              <span className={cn(
                'w-4 h-4 shrink-0 rounded text-[9px] font-bold flex items-center justify-center border',
                statusColor[f.status] ?? statusColor.modified,
              )}>
                {statusLabel[f.status] ?? 'M'}
              </span>
              <span className="truncate font-mono flex-1 text-left">{f.path.split('/').pop()}</span>
              <span className="shrink-0 text-[10px] text-muted-foreground/40 font-mono">
                {f.insertions > 0 && <span className="text-emerald-400/70">+{f.insertions}</span>}
                {f.insertions > 0 && f.deletions > 0 && ' '}
                {f.deletions > 0 && <span className="text-red-400/70">-{f.deletions}</span>}
              </span>
            </button>
          ))}
        </div>

        {result.truncated && (
          <div className="px-3 py-2 border-t border-white/8 text-[10px] text-amber-400/70 shrink-0">
            Diff truncated at 3000 lines
          </div>
        )}
      </div>

      {/* Diff content */}
      <div className="flex-1 overflow-auto">
        {filteredLines.length === 0 ? (
          <div className="flex items-center justify-center h-full text-xs text-muted-foreground/50">
            No diff content for this file
          </div>
        ) : (
          <table className="w-full border-collapse text-[12px] font-mono leading-[1.5]">
            <tbody>
              {filteredLines.map((line, i) => {
                let bg = '';
                let fg = 'text-muted-foreground/70';

                if (line.startsWith('+++') || line.startsWith('---')) {
                  fg = 'text-muted-foreground/50';
                  bg = 'bg-white/[0.02]';
                } else if (line.startsWith('diff --git') || line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file')) {
                  fg = 'text-sky-400/70';
                  bg = 'bg-sky-500/5';
                } else if (line.startsWith('@@')) {
                  fg = 'text-cyan-400/80';
                  bg = 'bg-cyan-500/5';
                } else if (line.startsWith('+')) {
                  fg = 'text-emerald-300';
                  bg = 'bg-emerald-500/10';
                } else if (line.startsWith('-')) {
                  fg = 'text-red-300';
                  bg = 'bg-red-500/10';
                }

                return (
                  <tr key={i} className={cn('group', bg)}>
                    <td className={cn('select-none w-10 pl-4 pr-2 text-right text-muted-foreground/25 border-r border-white/[0.04] sticky left-0', bg)}>
                      {i + 1}
                    </td>
                    <td className={cn('pl-4 pr-6 py-[1px] whitespace-pre', fg)}>
                      {line}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ─── Tree Node Item ───────────────────────────────────────────────────────────

interface TreeNodeItemProps {
  node: TreeNode;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  onSelect: (node: TreeNode) => void;
  selectedPath: string;
}

function TreeNodeItem({ node, depth, expanded, onToggle, onSelect, selectedPath }: TreeNodeItemProps) {
  const isExpanded = expanded.has(node.path);
  const indent = 8 + depth * 14;

  if (node.type === 'tree') {
    return (
      <div>
        <button
          onClick={() => onToggle(node.path)}
          className="flex items-center gap-1.5 w-full text-left py-[3px] text-xs text-muted-foreground hover:text-foreground hover:bg-white/5 rounded transition-colors"
          style={{ paddingLeft: `${indent}px`, paddingRight: '8px' }}
        >
          {isExpanded
            ? <ChevronDown className="w-3 h-3 shrink-0 text-muted-foreground/60" />
            : <ChevronRight className="w-3 h-3 shrink-0 text-muted-foreground/60" />}
          {isExpanded
            ? <FolderOpen className="w-3.5 h-3.5 shrink-0 text-yellow-400/80" />
            : <Folder className="w-3.5 h-3.5 shrink-0 text-yellow-400/60" />}
          <span className="truncate font-medium">{node.name}</span>
        </button>
        {isExpanded && node.children?.map(child => (
          <TreeNodeItem
            key={child.path}
            node={child}
            depth={depth + 1}
            expanded={expanded}
            onToggle={onToggle}
            onSelect={onSelect}
            selectedPath={selectedPath}
          />
        ))}
      </div>
    );
  }

  const isSelected = selectedPath === node.path;
  return (
    <button
      onClick={() => onSelect(node)}
      className={cn(
        'flex items-center gap-1.5 w-full text-left py-[3px] text-xs rounded transition-colors',
        isSelected
          ? 'bg-primary/15 text-primary'
          : 'text-muted-foreground hover:bg-white/5 hover:text-foreground',
      )}
      style={{ paddingLeft: `${indent + 14}px`, paddingRight: '8px' }}
    >
      <FileIcon filename={node.name} />
      <span className="truncate">{node.name}</span>
    </button>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

interface FileExplorerModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  projectId: string;
}

type ModalMode = 'browse' | 'compare';

export function FileExplorerModal({ open, onOpenChange, projectName, projectId }: FileExplorerModalProps) {
  const [mode, setMode] = useState<ModalMode>('browse');

  // ── Shared branch state ──────────────────────────────────────────────────────
  const [branches, setBranches] = useState<string[]>([]);
  const [branch, setBranch] = useState('');
  const [noGitMessage, setNoGitMessage] = useState<string | null>(null);
  const [branchesFetchError, setBranchesFetchError] = useState('');

  // ── Browse-mode state ────────────────────────────────────────────────────────
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [treeLoading, setTreeLoading] = useState(false);
  const [treeError, setTreeError] = useState('');
  const [selectedPath, setSelectedPath] = useState('');
  const [fileContent, setFileContent] = useState('');
  const [highlightedHtml, setHighlightedHtml] = useState('');
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState('');
  const [fileLang, setFileLang] = useState('');
  const [isBinary, setIsBinary] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const codeRef = useRef<HTMLDivElement>(null);

  // ── Compare-mode state ───────────────────────────────────────────────────────
  const [compareBranch, setCompareBranch] = useState('');
  const [diffResult, setDiffResult] = useState<DiffResult | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState('');

  // ── Fetch branches on open ───────────────────────────────────────────────────
  useEffect(() => {
    if (!open || !projectId) return;
    setBranches([]);
    setBranch('');
    setCompareBranch('');
    setNoGitMessage(null);
    setBranchesFetchError('');
    fetch(`/api/repos/${encodeURIComponent(projectId)}/branches`)
      .then(async (r) => {
        const data = (await r.json().catch(() => ({}))) as {
          default?: string;
          branches?: string[];
          gitAvailable?: boolean;
          message?: string;
          error?: string;
        };
        if (!r.ok) {
          throw new Error(data.error || `Failed to load repository (${r.status})`);
        }
        if (data.gitAvailable === false) {
          setNoGitMessage(
            data.message ||
              'This project is not a Git repository. Add one by creating the project with a clone URL or run git init in the project folder.',
          );
          return;
        }
        const list = data.branches ?? [];
        setBranches(list);
        setBranch(data.default ?? '');
        const second = list.find((b) => b !== (data.default ?? ''));
        setCompareBranch(second ?? data.default ?? '');
      })
      .catch((e: unknown) => {
        setBranchesFetchError(e instanceof Error ? e.message : 'Failed to load branches');
      });
  }, [open, projectId]);

  // ── Load tree when branch changes (browse mode) ──────────────────────────────
  useEffect(() => {
    if (!branch || !projectId || mode !== 'browse') return;
    setTreeLoading(true);
    setTreeError('');
    setTree([]);
    setSelectedPath('');
    setFileContent('');
    setHighlightedHtml('');
    setExpanded(new Set());
    setSearch('');
    setIsBinary(false);

    fetch(`/api/repos/${encodeURIComponent(projectId)}/tree?branch=${encodeURIComponent(branch)}`)
      .then(r => r.json())
      .then((data: { branch: string; entries: TreeEntry[]; error?: string }) => {
        if (data.error) throw new Error(data.error);
        setTree(buildTree(data.entries ?? []));
        setTreeLoading(false);
      })
      .catch(err => {
        setTreeError(err.message ?? 'Failed to load repository');
        setTreeLoading(false);
      });
  }, [branch, projectId, mode]);

  // ── Reset on close ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!open) {
      setBranches([]);
      setBranch('');
      setCompareBranch('');
      setNoGitMessage(null);
      setBranchesFetchError('');
      setTree([]);
      setSelectedPath('');
      setFileContent('');
      setHighlightedHtml('');
      setDiffResult(null);
      setDiffError('');
      setMode('browse');
    }
  }, [open]);

  // ── Load file ────────────────────────────────────────────────────────────────
  const loadFile = useCallback(
    async (node: TreeNode) => {
      if (node.type !== 'blob') return;
      setSelectedPath(node.path);
      setFileLoading(true);
      setFileError('');
      setFileContent('');
      setHighlightedHtml('');
      setIsBinary(false);

      const lang = getLanguage(node.name);
      setFileLang(lang);

      try {
        const params = new URLSearchParams({ path: node.path, branch });
        const r = await fetch(`/api/repos/${encodeURIComponent(projectId)}/file?${params}`);
        const data = await r.json();

        if (!r.ok || data.error) throw new Error(data.error ?? 'Failed to load file');

        if (data.binary) {
          setIsBinary(true);
        } else {
          const content: string = data.content ?? '';
          setFileContent(content);
          try {
            const result =
              lang !== 'plaintext' && hljs.getLanguage(lang)
                ? hljs.highlight(content, { language: lang })
                : hljs.highlightAuto(content);
            setHighlightedHtml(result.value);
          } catch {
            setHighlightedHtml(
              content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'),
            );
          }
        }
      } catch (err: unknown) {
        setFileError(err instanceof Error ? err.message : 'Failed to load file');
      } finally {
        setFileLoading(false);
      }
    },
    [projectId, branch],
  );

  // ── Run comparison ───────────────────────────────────────────────────────────
  const runCompare = async () => {
    if (!branch || !compareBranch || branch === compareBranch) return;
    setDiffLoading(true);
    setDiffError('');
    setDiffResult(null);
    try {
      const params = new URLSearchParams({ base: branch, head: compareBranch });
      const r = await fetch(`/api/repos/${encodeURIComponent(projectId)}/diff?${params}`);
      const data: DiffResult = await r.json();
      if (data.error) throw new Error(data.error);
      setDiffResult(data);
    } catch (err: unknown) {
      setDiffError(err instanceof Error ? err.message : 'Failed to compute diff');
    } finally {
      setDiffLoading(false);
    }
  };

  const toggleExpanded = (path: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const lineCount = fileContent ? fileContent.split('\n').length : 0;
  const displayedTree = search.trim() ? flattenTree(tree, search.trim()) : null;
  const sameBranch = branch === compareBranch;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          className={cn(
            'fixed left-[50%] top-[50%] z-50 translate-x-[-50%] translate-y-[-50%]',
            'w-[calc(100vw-32px)] h-[calc(100vh-48px)] max-w-[1400px]',
            'bg-[#0d1117] border border-white/10 rounded-xl shadow-2xl overflow-hidden',
            'data-[state=open]:animate-in data-[state=closed]:animate-out',
            'data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0',
            'data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
            'data-[state=closed]:slide-out-to-left-1/2 data-[state=closed]:slide-out-to-top-[48%]',
            'data-[state=open]:slide-in-from-left-1/2 data-[state=open]:slide-in-from-top-[48%]',
            'flex flex-col',
          )}
        >
          <DialogPrimitive.Title className="sr-only">
            {mode === 'browse' ? 'File Explorer' : 'Branch Compare'} — {projectName}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Browse files in the project Git repository, compare branches, and view diffs. Use the branch
            selector when the project is a Git clone.
          </DialogPrimitive.Description>

          {/* ── Header ── */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-white/8 bg-white/[0.02] shrink-0 gap-3">
            <div className="flex items-center gap-3 min-w-0 flex-1">
              {/* Traffic lights */}
              <div className="flex gap-1.5 shrink-0">
                <span className="w-3 h-3 rounded-full bg-[#ff5f57]" />
                <span className="w-3 h-3 rounded-full bg-[#febc2e]" />
                <span className="w-3 h-3 rounded-full bg-[#28c840]" />
              </div>

              <span className="text-sm font-semibold text-foreground truncate shrink-0">{projectName}</span>

              {/* Mode switcher */}
              <div className="flex items-center gap-1 bg-white/5 border border-white/8 rounded-lg p-0.5 shrink-0">
                <button
                  onClick={() => setMode('browse')}
                  className={cn(
                    'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium transition-all',
                    mode === 'browse'
                      ? 'bg-primary/20 text-primary border border-primary/30'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  <FileCode className="w-3 h-3" />
                  Browse
                </button>
                <button
                  onClick={() => setMode('compare')}
                  className={cn(
                    'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium transition-all',
                    mode === 'compare'
                      ? 'bg-primary/20 text-primary border border-primary/30'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  <GitCompare className="w-3 h-3" />
                  Compare
                </button>
              </div>

              {/* Branch controls */}
              {mode === 'browse' && branches.length > 0 && (
                <BranchPicker branches={branches} value={branch} onChange={b => { setBranch(b); }} />
              )}

              {mode === 'compare' && branches.length > 0 && (
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-[10px] text-muted-foreground/50 shrink-0">Base</span>
                  <BranchPicker branches={branches} value={branch} onChange={setBranch} />
                  <ArrowRight className="w-3.5 h-3.5 text-muted-foreground/40 shrink-0" />
                  <span className="text-[10px] text-muted-foreground/50 shrink-0">Compare</span>
                  <BranchPicker branches={branches} value={compareBranch} onChange={setCompareBranch} />
                  <button
                    onClick={runCompare}
                    disabled={diffLoading || sameBranch || !branch || !compareBranch}
                    className={cn(
                      'flex items-center gap-1.5 px-3 py-1 rounded-lg text-[11px] font-medium transition-all shrink-0',
                      'bg-primary/20 border border-primary/30 text-primary hover:bg-primary/30',
                      (diffLoading || sameBranch || !branch || !compareBranch) && 'opacity-50 cursor-not-allowed',
                    )}
                  >
                    {diffLoading
                      ? <Loader2 className="w-3 h-3 animate-spin" />
                      : <GitCompareArrows className="w-3 h-3" />}
                    Compare
                  </button>
                  {sameBranch && branch && (
                    <span className="text-[10px] text-amber-400/70 shrink-0">Same branch selected</span>
                  )}
                </div>
              )}
            </div>

            <DialogPrimitive.Close className="rounded-md p-1.5 text-muted-foreground hover:text-foreground hover:bg-white/10 transition-colors shrink-0">
              <X className="w-4 h-4" />
              <span className="sr-only">Close</span>
            </DialogPrimitive.Close>
          </div>

          {(noGitMessage || branchesFetchError) && (
            <div
              className={cn(
                'shrink-0 px-4 py-2.5 text-xs border-b flex items-start gap-2',
                branchesFetchError
                  ? 'border-destructive/30 bg-destructive/10 text-destructive'
                  : 'border-amber-500/30 bg-amber-500/10 text-amber-100',
              )}
            >
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span className="leading-relaxed">{branchesFetchError || noGitMessage}</span>
            </div>
          )}

          {/* ── Main Content ── */}
          <div className="flex-1 overflow-hidden">
            {/* ── Browse mode ── */}
            {mode === 'browse' && (
              <PanelGroup direction="horizontal" className="h-full">
                {/* File Tree */}
                <Panel defaultSize={22} minSize={14} maxSize={40}>
                  <div className="h-full flex flex-col border-r border-white/8">
                    <div className="px-2 py-2 border-b border-white/8 shrink-0">
                      <div className="relative">
                        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground/50" />
                        <input
                          type="text"
                          placeholder="Search files…"
                          value={search}
                          onChange={e => setSearch(e.target.value)}
                          className="w-full pl-8 pr-3 py-1.5 text-xs rounded-md bg-white/5 border border-white/8 text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:border-primary/40 transition-colors"
                        />
                      </div>
                    </div>

                    <div className="flex-1 overflow-y-auto py-1 scrollbar-thin">
                      {treeLoading && (
                        <div className="flex flex-col items-center justify-center h-full gap-2 text-muted-foreground">
                          <Loader2 className="w-5 h-5 animate-spin" />
                          <span className="text-xs">Loading tree…</span>
                        </div>
                      )}
                      {treeError && (
                        <div className="flex flex-col items-center justify-center h-full gap-2 text-destructive px-4 text-center">
                          <AlertCircle className="w-5 h-5" />
                          <span className="text-xs">{treeError}</span>
                        </div>
                      )}
                      {!treeLoading && !treeError && (
                        displayedTree ? (
                          displayedTree.length === 0 ? (
                            <div className="px-4 py-8 text-center text-xs text-muted-foreground">
                              No files match "{search}"
                            </div>
                          ) : (
                            displayedTree.map(node => (
                              <button
                                key={node.path}
                                onClick={() => loadFile(node)}
                                className={cn(
                                  'flex items-center gap-1.5 w-full text-left py-[3px] px-3 text-xs rounded transition-colors',
                                  selectedPath === node.path
                                    ? 'bg-primary/15 text-primary'
                                    : 'text-muted-foreground hover:bg-white/5 hover:text-foreground',
                                )}
                              >
                                <FileIcon filename={node.name} />
                                <span className="truncate">{node.name}</span>
                                <span className="ml-auto text-[10px] text-muted-foreground/40 truncate shrink-0 max-w-[60%] text-right">
                                  {node.path.split('/').slice(0, -1).join('/')}
                                </span>
                              </button>
                            ))
                          )
                        ) : (
                          tree.map(node => (
                            <TreeNodeItem
                              key={node.path}
                              node={node}
                              depth={0}
                              expanded={expanded}
                              onToggle={toggleExpanded}
                              onSelect={loadFile}
                              selectedPath={selectedPath}
                            />
                          ))
                        )
                      )}
                    </div>
                  </div>
                </Panel>

                <PanelResizeHandle className="w-[3px] bg-white/5 hover:bg-primary/40 transition-colors cursor-col-resize" />

                {/* Code Viewer */}
                <Panel minSize={30}>
                  <div className="h-full flex flex-col">
                    {selectedPath && (
                      <div className="flex items-center gap-3 px-4 py-2 border-b border-white/8 bg-white/[0.02] shrink-0">
                        <div className="flex items-center gap-2 min-w-0 flex-1">
                          <FileIcon filename={selectedPath.split('/').pop() ?? ''} />
                          {selectedPath.includes('/') && (
                            <span className="text-xs text-muted-foreground/60 truncate">
                              {selectedPath.split('/').slice(0, -1).join(' / ')}
                              <span className="text-muted-foreground/40"> / </span>
                            </span>
                          )}
                          <span className="text-xs font-medium text-foreground shrink-0">
                            {selectedPath.split('/').pop()}
                          </span>
                        </div>
                        {fileLang && fileLang !== 'plaintext' && (
                          <span className="text-[10px] px-2 py-0.5 rounded-full bg-white/5 border border-white/10 text-muted-foreground shrink-0">
                            {fileLang}
                          </span>
                        )}
                        {!fileLoading && lineCount > 0 && (
                          <span className="text-[10px] text-muted-foreground/40 shrink-0">
                            {lineCount.toLocaleString()} lines
                          </span>
                        )}
                      </div>
                    )}

                    <div className="flex-1 overflow-hidden relative">
                      {!selectedPath && !treeLoading && !treeError && (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground select-none">
                          <FileCode className="w-10 h-10 opacity-20" />
                          <p className="text-sm opacity-50">Select a file to view its contents</p>
                        </div>
                      )}
                      {fileLoading && (
                        <div className="flex items-center justify-center h-full gap-2 text-muted-foreground">
                          <Loader2 className="w-5 h-5 animate-spin" />
                          <span className="text-xs">Loading…</span>
                        </div>
                      )}
                      {fileError && (
                        <div className="flex flex-col items-center justify-center h-full gap-2 text-destructive px-4 text-center">
                          <AlertCircle className="w-5 h-5" />
                          <span className="text-xs">{fileError}</span>
                        </div>
                      )}
                      {!fileLoading && !fileError && isBinary && (
                        <div className="flex flex-col items-center justify-center h-full gap-3 text-muted-foreground select-none">
                          <File className="w-10 h-10 opacity-20" />
                          <p className="text-sm opacity-50">Binary file — cannot display</p>
                        </div>
                      )}
                      {!fileLoading && !fileError && !isBinary && highlightedHtml && (
                        <div ref={codeRef} className="h-full overflow-auto">
                          <div className="flex min-w-max">
                            <div
                              className="select-none text-right pr-4 pl-4 py-4 text-[12px] font-mono leading-[1.6] text-muted-foreground/25 bg-[#0d1117] sticky left-0 z-10 border-r border-white/[0.04]"
                              aria-hidden="true"
                            >
                              {Array.from({ length: lineCount }, (_, i) => (
                                <div key={i}>{i + 1}</div>
                              ))}
                            </div>
                            <pre className="py-4 pl-5 pr-8 m-0 text-[12px] font-mono leading-[1.6] bg-transparent flex-1">
                              <code
                                dangerouslySetInnerHTML={{ __html: highlightedHtml }}
                                className="hljs"
                                style={{ background: 'transparent', padding: 0 }}
                              />
                            </pre>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </Panel>
              </PanelGroup>
            )}

            {/* ── Compare mode ── */}
            {mode === 'compare' && (
              <DiffViewer result={diffResult} loading={diffLoading} error={diffError} />
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
