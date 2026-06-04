import React, { useEffect, useRef, useState, useMemo } from "react";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { ScrollArea } from "@/components/ui/scroll-area";
import { motion, AnimatePresence } from "framer-motion";
import {
  Loader2,
  Brain,
  Search,
  ChevronDown,
  ChevronRight,
  Zap,
  FileCode,
  Terminal,
  PenLine,
  Database,
  BookOpen,
  CheckCircle2,
  XCircle,
  Maximize2,
  Minimize2,
  Clock,
  Activity,
  Copy,
  Check,
  Braces,
  Shield,
  Trash2,
  Network,
  Cpu,
  BarChart3,
  Download,
  Filter,
  ArrowDownCircle,
  Sparkles,
} from "lucide-react";
import { createLogger } from '@/utils/logger';

const log = createLogger('components.AgentThoughtDrawer');

// ─── Types ───────────────────────────────────────────────────────────────────

interface AgentThoughtDrawerProps {
  taskId: string | null;
  taskTitle?: string;
  taskStatus?: string;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

interface ThoughtEntry {
  ts: string;
  thought: string;
  agent_role?: string;
  meta?: Record<string, any>;
}

type ThoughtCategory = "system" | "pm" | "implementer" | "critic" | "tester" | "sweeper" | "unknown";

interface ParsedThought {
  raw: ThoughtEntry;
  category: ThoughtCategory;
  cleanText: string;
  toolAction?: string;
  elapsedMs?: number;
  targetFile?: string;
}

// ─── Parsing Utilities ───────────────────────────────────────────────────────

function parseCategory(thought: string, agentRole?: string): { category: ThoughtCategory; cleanText: string } {
  let category: ThoughtCategory = "unknown";
  
  // 1. Direct role mapping (primary)
  if (agentRole) {
    const role = agentRole.toLowerCase();
    if (role === "system") category = "system";
    else if (role === "pm" || role === "pm_dispatcher" || role === "pm-dispatcher" || role === "planner" || role === "planning-router") category = "pm";
    else if (role === "implementer" || role === "go-worker" || role === "worker") category = "implementer";
    else if (role === "critic" || role === "reviewer" || role === "critic-agent") category = "critic";
    else if (role === "tester" || role === "test-runner") category = "tester";
    else if (role === "sweeper") category = "sweeper";
  }

  // 2. Legacy prefix mapping (fallback)
  let cleanText = thought;
  if (category === "unknown") {
    if (thought.startsWith("*[PM Dispatcher]*")) {
      category = "pm";
      cleanText = thought.replace("*[PM Dispatcher]*", "").trim();
    } else if (thought.startsWith("*[System]*")) {
      category = "system";
      cleanText = thought.replace("*[System]*", "").trim();
    } else if (thought.startsWith("*[Agent]*")) {
      category = "implementer";
      cleanText = thought.replace("*[Agent]*", "").trim();
    }
  }

  return { category, cleanText };
}

function extractToolAction(text: string): string | undefined {
  if (text.startsWith("Querying AST") || text.includes("query_ast")) return "AST Query";
  if (text.startsWith("Reading file:") || text.includes("view_file")) return "File Read";
  if (text.startsWith("Writing file:") || text.includes("write_to_file")) return "File Write";
  if (text.startsWith("Replacing content") || text.includes("replace_file_content") || text.includes("multi_replace_file_content")) return "Code Edit";
  if (text.startsWith("Listing directory:") || text.includes("list_dir")) return "Directory Scan";
  if (text.startsWith("Running:") || text.includes("run_command")) return "Shell Exec";
  if (text.startsWith("Reading memory:")) return "Memory Read";
  if (text.startsWith("Writing memory:")) return "Memory Write";
  if (text.startsWith("Completing:")) return "Complete";
  if (text.startsWith("Thinking…") || text.startsWith("Thinking")) return "Reasoning";
  if (text.startsWith("Agent started") || text.includes("claimed task")) return "Initialize";
  if (text.startsWith("Sending to LLM") || text.includes("LLM call")) return "LLM Call";
  if (text.startsWith("LLM returned") || text.includes("LLM failed")) return "LLM Error";
  if (text.includes("Decomposed into") || text.includes("Decomposing")) return "Decompose";
  if (text.includes("Decomposition failed") || text.includes("State SM Violation")) return "Error";
  return undefined;
}

function findFilePath(text: string, meta?: Record<string, any>): string | undefined {
  if (meta) {
    if (typeof meta.path === "string") return meta.path;
    if (typeof meta.file === "string") return meta.file;
    if (typeof meta.TargetFile === "string") return meta.TargetFile;
    if (typeof meta.TargetPath === "string") return meta.TargetPath;
  }
  
  // Regex search for workspace files in text
  const match = text.match(/(?:\/app\/workspace\/flume-reg-rflow\/[^\s\)\`]+|\/app\/[^\s\)\`]+|[a-zA-Z0-9_\-\.\/]+\.(?:go|py|ts|tsx|js|json|yml|yaml|md|sh|css|html))/);
  if (match) {
    let p = match[0];
    // Clean up surrounding quotes
    p = p.replace(/^["'`]/, "").replace(/["'`]$/, "");
    // Truncate long absolute path prefixes for clean UI presentation
    if (p.includes("/Users/jonathandoughty/clients/fremenlabs/")) {
      p = p.replace("/Users/jonathandoughty/clients/fremenlabs/", "");
    }
    return p;
  }
  return undefined;
}

// ─── Theme and Color Mappings ────────────────────────────────────────────────

const categoryConfig: Record<ThoughtCategory, {
  icon: React.ReactNode;
  label: string;
  accent: string;
  borderAccent: string;
  bg: string;
  border: string;
  glow: string;
  badge: string;
}> = {
  system: {
    icon: <Zap className="w-3.5 h-3.5" />,
    label: "System",
    accent: "text-cyan-400",
    borderAccent: "bg-cyan-500",
    bg: "bg-cyan-950/20 backdrop-blur-md",
    border: "border-cyan-500/20",
    glow: "shadow-[0_0_15px_-3px_rgba(34,211,238,0.1)]",
    badge: "bg-cyan-500/10 text-cyan-400 border-cyan-500/30",
  },
  pm: {
    icon: <Brain className="w-3.5 h-3.5" />,
    label: "Product Manager",
    accent: "text-violet-400",
    borderAccent: "bg-violet-500",
    bg: "bg-violet-950/20 backdrop-blur-md",
    border: "border-violet-500/20",
    glow: "shadow-[0_0_15px_-3px_rgba(167,139,250,0.1)]",
    badge: "bg-violet-500/10 text-violet-400 border-violet-500/30",
  },
  implementer: {
    icon: <FileCode className="w-3.5 h-3.5" />,
    label: "Implementer",
    accent: "text-emerald-400",
    borderAccent: "bg-emerald-500",
    bg: "bg-emerald-950/15 backdrop-blur-md",
    border: "border-emerald-500/20",
    glow: "shadow-[0_0_15px_-3px_rgba(52,211,153,0.1)]",
    badge: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  },
  critic: {
    icon: <Shield className="w-3.5 h-3.5" />,
    label: "Critic / Reviewer",
    accent: "text-rose-400",
    borderAccent: "bg-rose-500",
    bg: "bg-rose-950/20 backdrop-blur-md",
    border: "border-rose-500/20",
    glow: "shadow-[0_0_15px_-3px_rgba(251,113,133,0.1)]",
    badge: "bg-rose-500/10 text-rose-400 border-rose-500/30",
  },
  tester: {
    icon: <Activity className="w-3.5 h-3.5" />,
    label: "Tester",
    accent: "text-amber-400",
    borderAccent: "bg-amber-500",
    bg: "bg-amber-950/20 backdrop-blur-md",
    border: "border-amber-500/20",
    glow: "shadow-[0_0_15px_-3px_rgba(245,158,11,0.1)]",
    badge: "bg-amber-500/10 text-amber-400 border-amber-500/30",
  },
  sweeper: {
    icon: <Trash2 className="w-3.5 h-3.5" />,
    label: "Sweeper",
    accent: "text-slate-400",
    borderAccent: "bg-slate-500",
    bg: "bg-slate-950/20 backdrop-blur-md",
    border: "border-slate-800/80",
    glow: "shadow-none",
    badge: "bg-slate-500/10 text-slate-400 border-slate-500/20",
  },
  unknown: {
    icon: <BookOpen className="w-3.5 h-3.5" />,
    label: "Log",
    accent: "text-zinc-400",
    borderAccent: "bg-zinc-600",
    bg: "bg-zinc-950/10 backdrop-blur-md",
    border: "border-zinc-800/50",
    glow: "shadow-none",
    badge: "bg-zinc-800 text-zinc-400 border-zinc-700/50",
  },
};

const toolBadgeColors: Record<string, string> = {
  "AST Query": "bg-amber-500/10 text-amber-400 border-amber-500/20",
  "File Read": "bg-blue-500/10 text-blue-400 border-blue-500/20",
  "File Write": "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  "Code Edit": "bg-green-500/10 text-green-400 border-green-500/20",
  "Directory Scan": "bg-slate-500/10 text-slate-400 border-slate-500/20",
  "Shell Exec": "bg-orange-500/10 text-orange-400 border-orange-500/20",
  "Memory Read": "bg-purple-500/10 text-purple-400 border-purple-500/20",
  "Memory Write": "bg-purple-500/10 text-purple-400 border-purple-500/20",
  "Complete": "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
  "Reasoning": "bg-indigo-500/10 text-indigo-400 border-indigo-500/20",
  "Initialize": "bg-sky-500/10 text-sky-400 border-sky-500/20",
  "LLM Call": "bg-violet-500/10 text-violet-400 border-violet-500/20",
  "LLM Error": "bg-red-500/20 text-red-300 border-red-500/30",
  "Decompose": "bg-teal-500/10 text-teal-400 border-teal-500/20",
  "Error": "bg-red-500/20 text-red-300 border-red-500/30",
};

const toolIcons: Record<string, React.ReactNode> = {
  "AST Query": <Database className="w-3 h-3" />,
  "File Read": <FileCode className="w-3 h-3" />,
  "File Write": <PenLine className="w-3 h-3" />,
  "Code Edit": <PenLine className="w-3 h-3" />,
  "Shell Exec": <Terminal className="w-3 h-3" />,
  "Complete": <CheckCircle2 className="w-3 h-3" />,
  "Error": <XCircle className="w-3 h-3" />,
  "LLM Error": <XCircle className="w-3 h-3" />,
};

function formatElapsed(ms: number): string {
  if (ms < 1000) return `+${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `+${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `+${m}m${rem}s`;
}

function formatTotalElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m ${s % 60}s`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

// ─── Markdown with Syntax Highlighting ───────────────────────────────────────

function ThoughtMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      components={{
        code({ className, children, ...props }) {
          const match = /language-(\w+)/.exec(className || "");
          const inline = !match && !className;
          return !inline && match ? (
            <SyntaxHighlighter
              style={oneDark}
              language={match[1]}
              PreTag="div"
              customStyle={{
                margin: "0.5rem 0",
                borderRadius: "0.5rem",
                fontSize: "0.75rem",
                padding: "0.75rem",
                background: "#09090b",
                border: "1px solid #27272a",
              }}
            >
              {String(children).replace(/\n$/, "")}
            </SyntaxHighlighter>
          ) : (
            <code className="bg-zinc-900 text-zinc-300 border border-zinc-800 text-[11px] px-1.5 py-0.5 rounded font-mono font-semibold" {...props}>
              {children}
            </code>
          );
        },
        p({ children }) {
          return <p className="leading-relaxed text-[13px] text-zinc-300/90 mb-2">{children}</p>;
        },
        strong({ children }) {
          return <strong className="text-zinc-100 font-semibold">{children}</strong>;
        },
        ul({ children }) {
          return <ul className="list-disc list-inside space-y-1 text-[13px] pl-1 mb-2 text-zinc-300/90">{children}</ul>;
        },
        li({ children }) {
          return <li className="pl-1">{children}</li>;
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

// ─── Single Thought Entry Component ──────────────────────────────────────────

const ThoughtCard = React.memo(
  function ThoughtCard({ parsed, index, isLatest }: { parsed: ParsedThought; index: number; isLatest: boolean }) {
    const [collapsed, setCollapsed] = useState(parsed.cleanText.length > 450);
    const [showMeta, setShowMeta] = useState(false);
    const [copied, setCopied] = useState(false);

    const config = categoryConfig[parsed.category];
    const toolAction = parsed.toolAction;
    const isLongText = parsed.cleanText.length > 450;
    const hasMeta = parsed.raw.meta && Object.keys(parsed.raw.meta).length > 0;
    const targetFile = parsed.targetFile;

    const handleCopy = () => {
      navigator.clipboard.writeText(parsed.cleanText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    };

    // Smart telemetry summary inside card if available
    const cardTelemetry = useMemo(() => {
      if (!parsed.raw.meta) return null;
      const meta = parsed.raw.meta;
      const model = meta.model as string;
      const provider = meta.provider as string;
      const node = (meta.node_id || meta.node_host) as string;
      
      let tokens = 0;
      if (typeof meta.prompt_tokens === "number" && typeof meta.completion_tokens === "number") {
        tokens = meta.prompt_tokens + meta.completion_tokens;
      } else if (meta.usage && typeof meta.usage.prompt_tokens === "number" && typeof meta.usage.completion_tokens === "number") {
        tokens = meta.usage.prompt_tokens + meta.usage.completion_tokens;
      } else if (meta.usage && typeof meta.usage.PromptTokens === "number" && typeof meta.usage.CompletionTokens === "number") {
        tokens = meta.usage.PromptTokens + meta.usage.CompletionTokens;
      }
      
      if (!model && !provider && !node && tokens === 0) return null;
      return { model, provider, node, tokens };
    }, [parsed.raw.meta]);

    return (
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className={`relative rounded-xl border ${config.border} ${config.bg} ${config.glow} overflow-hidden transition-all duration-300 hover:border-zinc-700/60 hover:bg-zinc-900/40`}
      >
        {/* Header Bar */}
        <div className="flex items-center justify-between px-3.5 py-2.5 border-b border-zinc-800/40 bg-zinc-950/20">
          <div className="flex items-center gap-2">
            <span className={`${config.accent} flex items-center gap-1.5`}>
              {config.icon}
              <span className="text-[10px] font-bold uppercase tracking-wider">{config.label}</span>
            </span>
            {toolAction && (
              <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full border ${toolBadgeColors[toolAction] || "bg-zinc-800/50 text-zinc-400 border-zinc-700/30"}`}>
                {toolIcons[toolAction]}
                {toolAction}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            {hasMeta && (
              <button
                onClick={() => setShowMeta(!showMeta)}
                className={`p-1 rounded-lg hover:bg-zinc-800/50 text-zinc-500 hover:text-zinc-300 transition-colors ${showMeta ? 'text-violet-400 bg-violet-500/10 hover:text-violet-300' : ''}`}
                title="Inspect metadata parameters"
              >
                <Braces className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              onClick={handleCopy}
              className="p-1 rounded-lg hover:bg-zinc-800/50 text-zinc-500 hover:text-zinc-300 transition-colors"
              title="Copy thought text"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
            <div className="w-px h-3 bg-zinc-800" />
            <div className="flex items-center gap-1.5 font-mono text-[10px]">
              {parsed.elapsedMs !== undefined && parsed.elapsedMs > 0 && (
                <span className="text-zinc-500 font-semibold">
                  {formatElapsed(parsed.elapsedMs)}
                </span>
              )}
              <span className="text-zinc-600">
                {new Date(parsed.raw.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
            </div>
          </div>
        </div>

        {/* Content */}
        <div className="px-4 py-3.5">
          {/* Target File Link Badge */}
          {targetFile && (
            <div className="flex items-center gap-1.5 mb-2.5">
              <span className="text-[9px] uppercase tracking-wider text-zinc-500 font-bold">Target File:</span>
              <span className="inline-flex items-center gap-1 text-[11px] font-mono bg-zinc-900 border border-zinc-800 px-2 py-0.5 rounded text-cyan-400 font-medium select-all">
                <FileCode className="w-3.5 h-3.5 text-cyan-500" />
                {targetFile}
              </span>
            </div>
          )}

          <div className={`prose prose-invert max-w-none text-zinc-300/90 leading-relaxed ${isLongText && collapsed ? "max-h-24 overflow-hidden relative" : ""}`}>
            <ThoughtMarkdown content={isLongText && collapsed ? parsed.cleanText.slice(0, 420) + "…" : parsed.cleanText} />
            {isLongText && collapsed && (
              <div className="absolute bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-zinc-950/90 to-transparent" />
            )}
          </div>
          
          {isLongText && (
            <button
              onClick={() => setCollapsed(!collapsed)}
              className="flex items-center gap-1 mt-2.5 text-[11px] text-violet-400 hover:text-violet-300 font-semibold transition-colors bg-violet-500/5 px-2.5 py-1 rounded-lg border border-violet-500/10 hover:border-violet-500/20"
            >
              {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
              {collapsed ? "Expand full reasoning" : "Collapse"}
            </button>
          )}

          {/* Compact Telemetry Row inside card */}
          {cardTelemetry && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 mt-3 pt-3 border-t border-zinc-900 font-mono text-[10px] text-zinc-500">
              {cardTelemetry.model && (
                <span className="flex items-center gap-1">
                  <Cpu className="w-3 h-3 text-violet-500" />
                  {cardTelemetry.model}
                </span>
              )}
              {cardTelemetry.node && (
                <span className="flex items-center gap-1">
                  <Network className="w-3 h-3 text-cyan-500" />
                  {cardTelemetry.node}
                </span>
              )}
              {cardTelemetry.tokens > 0 && (
                <span className="flex items-center gap-1">
                  <Zap className="w-3 h-3 text-emerald-500" />
                  {cardTelemetry.tokens.toLocaleString()} tokens
                </span>
              )}
            </div>
          )}
        </div>

        {/* Metadata Inspect Panel */}
        {showMeta && hasMeta && (
          <div className="px-4 pb-4 border-t border-zinc-900 bg-zinc-950/60">
            <p className="text-[9px] font-bold text-zinc-500 mt-3 mb-2 uppercase tracking-wider">Metadata Parameters</p>
            <pre className="text-[10px] font-mono bg-zinc-950 p-3 rounded-lg border border-zinc-800 overflow-x-auto text-cyan-400/90 leading-relaxed max-h-48 scrollbar-thin scrollbar-thumb-zinc-800">
              {JSON.stringify(parsed.raw.meta, null, 2)}
            </pre>
          </div>
        )}

        {/* Step indicator left colored line */}
        <div className={`absolute left-0 top-0 bottom-0 w-[3px] ${config.borderAccent} opacity-80`} />
      </motion.div>
    );
  },
  (prevProps, nextProps) => {
    return (
      prevProps.parsed.cleanText === nextProps.parsed.cleanText &&
      prevProps.parsed.elapsedMs === nextProps.parsed.elapsedMs &&
      prevProps.isLatest === nextProps.isLatest &&
      prevProps.index === nextProps.index &&
      JSON.stringify(prevProps.parsed.raw.meta) === JSON.stringify(nextProps.parsed.raw.meta)
    );
  }
);

// ─── Thought Stream Content (shared between drawer and modal) ────────────────

function ThoughtStream({
  thoughts,
  isLoading,
  error,
  taskId,
  taskStatus,
  searchTerm,
  roleFilter,
}: {
  thoughts: ParsedThought[];
  isLoading: boolean;
  error: string | null;
  taskId: string | null;
  taskStatus?: string;
  searchTerm: string;
  roleFilter: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);
  const [renderLimit, setRenderLimit] = useState(45);

  const filtered = useMemo(() => {
    return thoughts.filter((t) => {
      // Role filter
      if (roleFilter !== "all" && t.category !== roleFilter) return false;
      
      // Text search
      if (!searchTerm) return true;
      const lower = searchTerm.toLowerCase();
      return (
        t.cleanText.toLowerCase().includes(lower) ||
        (t.toolAction || "").toLowerCase().includes(lower) ||
        t.category.toLowerCase().includes(lower) ||
        (t.targetFile || "").toLowerCase().includes(lower)
      );
    });
  }, [thoughts, searchTerm, roleFilter]);

  // Reset limit when taskId or search/filter changes
  useEffect(() => {
    setRenderLimit(45);
  }, [taskId, searchTerm, roleFilter]);

  const hasMore = filtered.length > renderLimit;
  const visibleThoughts = useMemo(() => {
    return hasMore ? filtered.slice(filtered.length - renderLimit) : filtered;
  }, [filtered, renderLimit, hasMore]);

  // Handle smooth scroll-to-bottom on new updates
  useEffect(() => {
    if (scrollRef.current && visibleThoughts.length > prevCountRef.current) {
      const el = scrollRef.current;
      setTimeout(() => {
        const viewport = el.querySelector('[data-radix-scroll-area-viewport]');
        if (viewport) {
          viewport.scrollTo({ top: viewport.scrollHeight, behavior: "smooth" });
        } else {
          el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        }
      }, 120);
    }
    prevCountRef.current = visibleThoughts.length;
  }, [visibleThoughts.length]);

  const isActive = taskStatus === "running";

  return (
    <div className="flex-1 overflow-hidden relative">
      <ScrollArea className="h-full" ref={scrollRef}>
        <div className="p-5 space-y-3.5">
          {isLoading && filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-48 text-zinc-500 gap-3 border border-dashed border-zinc-800 rounded-xl bg-zinc-950/40">
              <Loader2 className="w-5 h-5 animate-spin text-violet-500" />
              <p className="text-xs font-mono">Initializing EventSource connection stream…</p>
            </div>
          ) : error && filtered.length === 0 ? (
            <div className="text-rose-400 text-center text-sm mt-10 bg-rose-500/10 p-5 rounded-xl border border-rose-500/20 font-mono">
              <XCircle className="w-5 h-5 mx-auto mb-2 text-rose-500" />
              {error}
            </div>
          ) : filtered.length > 0 ? (
            <>
              {hasMore && (
                <button
                  onClick={() => setRenderLimit((prev) => prev + 50)}
                  className="w-full py-2.5 mb-3 text-xs font-semibold text-center text-violet-400 border border-dashed border-violet-500/20 rounded-xl bg-violet-500/5 hover:bg-violet-500/10 hover:text-violet-300 hover:border-violet-500/30 transition-all duration-200"
                >
                  Load older steps (+{filtered.length - renderLimit} remaining)
                </button>
              )}
              <AnimatePresence mode="popLayout">
                {visibleThoughts.map((entry, index) => (
                  <ThoughtCard
                    key={`${entry.raw.ts}-${index}`}
                    parsed={entry}
                    index={index}
                    isLatest={index === visibleThoughts.length - 1}
                  />
                ))}
              </AnimatePresence>
            </>
          ) : (
            <div className="flex flex-col items-center justify-center h-48 text-zinc-500 border border-dashed border-zinc-800 rounded-xl bg-zinc-950/20 gap-3.5">
              <Brain className="w-8 h-8 text-zinc-800" />
              <p className="text-xs font-medium text-zinc-400">
                {searchTerm || roleFilter !== "all" 
                  ? "No matching thoughts found." 
                  : "No reasoning steps recorded yet."}
              </p>
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Pulsing Live Indicator */}
      {isActive && filtered.length > 0 && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2">
          <div className="flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-zinc-950/95 border border-zinc-800/80 backdrop-blur-md shadow-2xl shadow-emerald-500/10">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
            </span>
            <span className="text-[10px] text-emerald-400 font-bold uppercase tracking-widest">Live Stream Active</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Header Stats & Telemetry Dashboard Panel ───────────────────────────────

function TelemetryDashboard({ summary, elapsedMs }: { summary: any; elapsedMs: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="border-b border-zinc-800/80 bg-zinc-950/45 p-4 overflow-hidden"
    >
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {/* Card 1: Time */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 p-3 rounded-xl flex flex-col justify-between">
          <span className="text-[10px] uppercase font-bold text-zinc-500 flex items-center gap-1">
            <Clock className="w-3.5 h-3.5 text-cyan-400" /> Elapsed Time
          </span>
          <span className="text-base font-bold text-zinc-100 font-mono mt-1.5">{formatTotalElapsed(elapsedMs)}</span>
        </div>
        
        {/* Card 2: Steps */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 p-3 rounded-xl flex flex-col justify-between">
          <span className="text-[10px] uppercase font-bold text-zinc-500 flex items-center gap-1">
            <Activity className="w-3.5 h-3.5 text-violet-400" /> Steps Logged
          </span>
          <span className="text-base font-bold text-zinc-100 font-mono mt-1.5">
            {summary.errors > 0 ? (
              <span className="flex items-center gap-1.5">
                {summary.totalSteps} 
                <span className="text-[10px] bg-rose-500/10 text-rose-400 border border-rose-500/20 px-1.5 py-0.5 rounded font-mono font-normal">
                  {summary.errors} err
                </span>
              </span>
            ) : (
              summary.totalSteps
            )}
          </span>
        </div>
        
        {/* Card 3: LLM Calls & Tokens */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 p-3 rounded-xl flex flex-col justify-between">
          <span className="text-[10px] uppercase font-bold text-zinc-500 flex items-center gap-1">
            <Sparkles className="w-3.5 h-3.5 text-emerald-400" /> LLM Inference
          </span>
          <div className="flex flex-col mt-1.5">
            <span className="text-sm font-bold text-zinc-200">
              {summary.llmCalls} calls
            </span>
            {summary.totalTokens > 0 && (
              <span className="text-[10px] text-zinc-500 font-mono">
                {summary.totalTokens.toLocaleString()} tokens
              </span>
            )}
          </div>
        </div>

        {/* Card 4: Mesh Models */}
        <div className="bg-zinc-900/40 border border-zinc-800/60 p-3 rounded-xl flex flex-col justify-between">
          <span className="text-[10px] uppercase font-bold text-zinc-500 flex items-center gap-1">
            <Network className="w-3.5 h-3.5 text-amber-400" /> Node Mesh
          </span>
          <div className="flex flex-col mt-1.5 truncate">
            {summary.modelsCount > 0 ? (
              <span className="text-xs font-bold text-zinc-200 truncate" title={summary.modelsList.join(", ")}>
                {summary.modelsList[0]}
              </span>
            ) : (
              <span className="text-xs text-zinc-500">Local Only</span>
            )}
            <span className="text-[9px] text-zinc-500 font-mono">
              {summary.nodesCount > 0 ? `${summary.nodesCount} nodes active` : "direct provider"}
            </span>
          </div>
        </div>
      </div>

      {/* Token Distribution Bar */}
      {summary.totalTokens > 0 && (
        <div className="mt-3.5 pt-3 border-t border-zinc-900 flex flex-col gap-1.5 font-mono text-[9px] text-zinc-500">
          <div className="flex justify-between font-bold">
            <span className="text-zinc-400">Prompt: {summary.inputTokens.toLocaleString()} ({Math.round(summary.inputTokens/summary.totalTokens*100)}%)</span>
            <span className="text-violet-400">Completion: {summary.outputTokens.toLocaleString()} ({Math.round(summary.outputTokens/summary.totalTokens*100)}%)</span>
          </div>
          <div className="w-full h-1.5 bg-zinc-900 rounded-full overflow-hidden flex border border-zinc-800/50">
            <div className="bg-cyan-500" style={{ width: `${summary.inputTokens/summary.totalTokens*100}%` }} />
            <div className="bg-violet-500" style={{ width: `${summary.outputTokens/summary.totalTokens*100}%` }} />
          </div>
        </div>
      )}
    </motion.div>
  );
}

// ─── Stats and Actions Bar ───────────────────────────────────────────────────

function StatsBar({
  totalSteps,
  totalElapsedMs,
  searchTerm,
  onSearchChange,
  roleFilter,
  onRoleFilterChange,
  isFullPage,
  onToggleMode,
  showTelemetry,
  onToggleTelemetry,
  onExport,
}: {
  totalSteps: number;
  totalElapsedMs: number;
  searchTerm: string;
  onSearchChange: (v: string) => void;
  roleFilter: string;
  onRoleFilterChange: (v: string) => void;
  isFullPage: boolean;
  onToggleMode: () => void;
  showTelemetry: boolean;
  onToggleTelemetry: () => void;
  onExport: () => void;
}) {
  return (
    <div className="flex flex-col border-b border-zinc-800 bg-zinc-900/10">
      {/* Search and Action Bar */}
      <div className="flex flex-wrap items-center gap-3 px-5 py-3 border-b border-zinc-900">
        {/* Toggle Telemetry */}
        <button
          onClick={onToggleTelemetry}
          className={`flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border transition-all ${
            showTelemetry 
              ? "bg-violet-500/15 border-violet-500/35 text-violet-300" 
              : "bg-zinc-900/50 border-zinc-800 text-zinc-400 hover:text-zinc-200"
          }`}
          title="Toggle telemetry summary dashboard"
        >
          <BarChart3 className="w-3.5 h-3.5" />
          Dashboard
        </button>

        {/* Export Logs */}
        <button
          onClick={onExport}
          className="flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1.5 rounded-lg border border-zinc-800 bg-zinc-900/50 text-zinc-400 hover:text-zinc-200 transition-colors"
          title="Export thought log to Markdown file"
        >
          <Download className="w-3.5 h-3.5" />
          Export
        </button>

        <div className="flex-1 min-w-[20px]" />

        {/* Filter Input */}
        <div className="relative">
          <Search className="w-3 h-3 text-zinc-600 absolute left-2.5 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            value={searchTerm}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Filter thought logs…"
            className="w-36 focus:w-56 transition-all text-xs bg-zinc-950/80 border border-zinc-800 rounded-lg pl-8 pr-2.5 py-1.5 text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-violet-500/55"
          />
        </div>

        {/* Maximize Drawer */}
        <button
          onClick={onToggleMode}
          className="p-1.5 rounded-lg border border-zinc-800 bg-zinc-900/50 text-zinc-500 hover:text-zinc-300 transition-colors"
          title={isFullPage ? "Switch to drawer view" : "Maximize to full-page view"}
        >
          {isFullPage ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
        </button>
      </div>

      {/* Role Filtering Tabs */}
      <div className="flex items-center gap-1.5 px-5 py-2 overflow-x-auto scrollbar-none border-b border-zinc-950/40 bg-zinc-950/10">
        <span className="text-[10px] uppercase font-bold text-zinc-600 flex items-center gap-1 mr-1.5 shrink-0">
          <Filter className="w-3 h-3" /> Filter:
        </span>
        {(["all", "system", "pm", "implementer", "critic", "tester", "sweeper"] as const).map((role) => {
          const config = categoryConfig[role === "all" ? "unknown" : role];
          const isSelected = roleFilter === role;
          const label = role === "all" ? "All Logs" : role === "pm" ? "PM" : role.charAt(0).toUpperCase() + role.slice(1);
          return (
            <button
              key={role}
              onClick={() => onRoleFilterChange(role)}
              className={`text-[10px] font-bold px-2.5 py-1 rounded-full border transition-all shrink-0 ${
                isSelected
                  ? `${config.badge} scale-102 font-extrabold shadow-sm`
                  : "bg-transparent border-transparent text-zinc-500 hover:text-zinc-300"
              }`}
            >
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ─── Main Drawer Component ───────────────────────────────────────────────────

export function AgentThoughtDrawer({ taskId, taskTitle, taskStatus, isOpen, onOpenChange }: AgentThoughtDrawerProps) {
  const [isFullPage, setIsFullPage] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [roleFilter, setRoleFilter] = useState("all");
  const [showTelemetry, setShowTelemetry] = useState(true);
  const [thoughts, setThoughts] = useState<ThoughtEntry[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId || !isOpen) {
      setThoughts([]);
      setError(null);
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setError(null);
    setThoughts([]);

    const eventSource = new EventSource(`/api/tasks/${taskId}/thoughts/stream`);

    eventSource.onmessage = (event) => {
      setIsLoading(false);
      try {
        const entry = JSON.parse(event.data) as ThoughtEntry;
        setThoughts((prev) => {
          // Deduplicate thoughts by checking timestamp and content
          if (prev.some((p) => p.ts === entry.ts && p.thought === entry.thought)) {
            return prev;
          }
          return [...prev, entry];
        });
      } catch (err) {
        log.error("SSE parse error", err);
      }
    };

    eventSource.onerror = (err) => {
      if (eventSource.readyState === EventSource.CLOSED) {
        setIsLoading(false);
        setError("Connection lost. Reconnecting...");
      }
    };

    return () => {
      eventSource.close();
    };
  }, [taskId, isOpen]);

  // Parse and enrich thought entries
  const parsedThoughts: ParsedThought[] = useMemo(() => {
    const raw = thoughts;
    return raw.map((entry, index) => {
      const { category, cleanText } = parseCategory(entry.thought, entry.agent_role);
      const toolAction = extractToolAction(cleanText);
      const targetFile = findFilePath(cleanText, entry.meta);
      const elapsedMs =
        index > 0
          ? new Date(entry.ts).getTime() - new Date(raw[index - 1].ts).getTime()
          : undefined;
      return { raw: entry, category, cleanText, toolAction, elapsedMs, targetFile };
    });
  }, [thoughts]);

  const totalElapsedMs = useMemo(() => {
    if (parsedThoughts.length < 2) return 0;
    return (
      new Date(parsedThoughts[parsedThoughts.length - 1].raw.ts).getTime() -
      new Date(parsedThoughts[0].raw.ts).getTime()
    );
  }, [parsedThoughts]);

  // Telemetry Aggregator Summary
  const telemetrySummary = useMemo(() => {
    let llmCalls = 0;
    let inputTokens = 0;
    let outputTokens = 0;
    let errors = 0;
    const nodesUsed = new Set<string>();
    const modelsUsed = new Set<string>();

    parsedThoughts.forEach((t) => {
      const meta = t.raw.meta || {};
      
      // Check if it's an LLM call
      const hasLLMMeta = meta.model || meta.provider || meta.usage || meta.prompt_tokens || meta.PromptTokens;
      if (hasLLMMeta || t.toolAction === "LLM Call") {
        llmCalls++;
      }

      if (meta.model) modelsUsed.add(meta.model as string);
      if (meta.node_id) nodesUsed.add(meta.node_id as string);
      else if (meta.node_host) nodesUsed.add(meta.node_host as string);

      // Extract tokens
      let pTokens = 0;
      let cTokens = 0;
      if (typeof meta.prompt_tokens === "number") pTokens = meta.prompt_tokens;
      else if (typeof meta.PromptTokens === "number") pTokens = meta.PromptTokens;
      else if (meta.usage && typeof meta.usage.prompt_tokens === "number") pTokens = meta.usage.prompt_tokens;
      else if (meta.usage && typeof meta.usage.PromptTokens === "number") pTokens = meta.usage.PromptTokens;

      if (typeof meta.completion_tokens === "number") cTokens = meta.completion_tokens;
      else if (typeof meta.CompletionTokens === "number") cTokens = meta.CompletionTokens;
      else if (meta.usage && typeof meta.usage.completion_tokens === "number") cTokens = meta.usage.completion_tokens;
      else if (meta.usage && typeof meta.usage.CompletionTokens === "number") cTokens = meta.usage.CompletionTokens;

      inputTokens += pTokens;
      outputTokens += cTokens;

      if (t.toolAction === "Error" || t.toolAction === "LLM Error" || meta.error || meta.violation === true) {
        errors++;
      }
    });

    return {
      totalSteps: parsedThoughts.length,
      llmCalls,
      inputTokens,
      outputTokens,
      totalTokens: inputTokens + outputTokens,
      errors,
      nodesCount: nodesUsed.size,
      modelsCount: modelsUsed.size,
      modelsList: Array.from(modelsUsed),
      nodesList: Array.from(nodesUsed),
    };
  }, [parsedThoughts]);

  // Reset state when closing
  useEffect(() => {
    if (!isOpen) {
      setSearchTerm("");
      setRoleFilter("all");
    }
  }, [isOpen]);

  // Export Log to Markdown
  const handleExportLogs = () => {
    if (parsedThoughts.length === 0) return;
    
    const mdHeader = `# Flume Agent Reasoning Log - Task ${taskId}\n` +
      `*Generated on: ${new Date().toLocaleString()}*\n` +
      `*Task Title: ${taskTitle || "N/A"}*\n` +
      `*Steps: ${telemetrySummary.totalSteps} | LLM Calls: ${telemetrySummary.llmCalls} | Total Tokens: ${telemetrySummary.totalTokens.toLocaleString()}*\n\n` +
      `---\n\n`;

    const mdContent = parsedThoughts.map((t, i) => {
      const config = categoryConfig[t.category];
      const timeStr = new Date(t.raw.ts).toLocaleTimeString();
      let header = `## Step ${i + 1}: [${config.label}] (${timeStr}`;
      if (t.elapsedMs) header += `, +${formatElapsed(t.elapsedMs)}`;
      header += `)\n`;
      
      if (t.toolAction) header += `*Action: ${t.toolAction}*\n`;
      if (t.targetFile) header += `*File: ${t.targetFile}*\n`;
      
      let body = `\n${t.cleanText}\n`;
      if (t.raw.meta && Object.keys(t.raw.meta).length > 0) {
        body += `\n\`\`\`json\n// Metadata Parameters\n${JSON.stringify(t.raw.meta, null, 2)}\n\`\`\`\n`;
      }
      return header + body + `\n---\n`;
    }).join("\n");

    const blob = new Blob([mdHeader + mdContent], { type: "text/markdown;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", `agent-reasoning-task-${taskId}.md`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const headerContent = (
    <div className="flex items-start justify-between w-full pr-6 text-zinc-100">
      <div className="flex items-center gap-3">
        <div className="p-2 rounded-xl bg-violet-500/10 border border-violet-500/20 shadow-[0_0_15px_-3px_rgba(139,92,246,0.2)]">
          <Brain className="w-5 h-5 text-violet-400" />
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-bold tracking-tight text-zinc-100 sm:text-base">Agent Reasoning Console</h3>
            <span className="text-[9px] bg-zinc-900 border border-zinc-800 text-zinc-500 px-2 py-0.5 rounded-full font-mono uppercase font-bold select-all">
              {taskId}
            </span>
          </div>
          {taskTitle && (
            <p className="text-xs text-zinc-400 truncate max-w-[280px] sm:max-w-[420px] mt-0.5">{taskTitle}</p>
          )}
        </div>
      </div>
    </div>
  );

  const sharedContent = (
    <>
      <StatsBar
        totalSteps={parsedThoughts.length}
        totalElapsedMs={totalElapsedMs}
        searchTerm={searchTerm}
        onSearchChange={setSearchTerm}
        roleFilter={roleFilter}
        onRoleFilterChange={setRoleFilter}
        isFullPage={isFullPage}
        onToggleMode={() => setIsFullPage(!isFullPage)}
        showTelemetry={showTelemetry}
        onToggleTelemetry={() => setShowTelemetry(!showTelemetry)}
        onExport={handleExportLogs}
      />
      
      {showTelemetry && parsedThoughts.length > 0 && (
        <TelemetryDashboard summary={telemetrySummary} elapsedMs={totalElapsedMs} />
      )}

      <ThoughtStream
        thoughts={parsedThoughts}
        isLoading={isLoading}
        error={error}
        taskId={taskId}
        taskStatus={taskStatus}
        searchTerm={searchTerm}
        roleFilter={roleFilter}
      />
    </>
  );

  // ─── Full-Page Modal ─────────────────────────────────────────────────────
  if (isFullPage) {
    return (
      <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-[95vw] w-[95vw] sm:max-w-[90vw] sm:w-[90vw] h-[90vh] flex flex-col p-0 gap-0 overflow-hidden border-zinc-800 bg-zinc-950/98 backdrop-blur-xl shadow-2xl">
          <DialogHeader className="px-6 py-4.5 border-b border-zinc-800 shrink-0 bg-zinc-950">
            <DialogTitle asChild>{headerContent}</DialogTitle>
            <DialogDescription className="sr-only">Live interactive agent reasoning, model telemetry, and execution logs console</DialogDescription>
          </DialogHeader>
          <div className="flex-1 flex flex-col min-h-0">
            {sharedContent}
          </div>
        </DialogContent>
      </Dialog>
    );
  }

  // ─── Side Drawer ─────────────────────────────────────────────────────────
  return (
    <Sheet open={isOpen} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-[90vw] sm:max-w-[620px] flex flex-col p-0 h-full border-l border-zinc-800/80 shadow-2xl bg-zinc-950/98 backdrop-blur-xl"
      >
        <SheetHeader className="px-6 py-4 border-b border-zinc-800 shrink-0 bg-zinc-950">
          <SheetTitle asChild>{headerContent}</SheetTitle>
          <SheetDescription className="sr-only">Live interactive agent reasoning, model telemetry, and execution logs console</SheetDescription>
        </SheetHeader>
        <div className="flex-1 flex flex-col min-h-0 bg-zinc-950/20">
          {sharedContent}
        </div>
      </SheetContent>
    </Sheet>
  );
}
