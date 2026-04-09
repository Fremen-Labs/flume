import React, { useEffect, useRef, useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
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
} from "lucide-react";

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
}

type ThoughtCategory = "system" | "pm_dispatcher" | "agent" | "unknown";

interface ParsedThought {
  raw: ThoughtEntry;
  category: ThoughtCategory;
  cleanText: string;
  toolAction?: string;
  elapsedMs?: number;
}

// ─── Parsing Utilities ───────────────────────────────────────────────────────

function parseCategory(thought: string): { category: ThoughtCategory; cleanText: string } {
  if (thought.startsWith("*[PM Dispatcher]*")) {
    return { category: "pm_dispatcher", cleanText: thought.replace("*[PM Dispatcher]*", "").trim() };
  }
  if (thought.startsWith("*[System]*")) {
    return { category: "system", cleanText: thought.replace("*[System]*", "").trim() };
  }
  if (thought.startsWith("*[Agent]*")) {
    return { category: "agent", cleanText: thought.replace("*[Agent]*", "").trim() };
  }
  return { category: "unknown", cleanText: thought };
}

function extractToolAction(text: string): string | undefined {
  if (text.startsWith("Querying AST")) return "AST Query";
  if (text.startsWith("Reading file:")) return "File Read";
  if (text.startsWith("Writing file:")) return "File Write";
  if (text.startsWith("Replacing content")) return "Code Edit";
  if (text.startsWith("Listing directory:")) return "Directory Scan";
  if (text.startsWith("Running:")) return "Shell Exec";
  if (text.startsWith("Reading memory:")) return "Memory Read";
  if (text.startsWith("Writing memory:")) return "Memory Write";
  if (text.startsWith("Completing:")) return "Complete";
  if (text.startsWith("Thinking…")) return "Reasoning";
  if (text.startsWith("Agent started")) return "Initialize";
  if (text.startsWith("Sending to LLM")) return "LLM Call";
  if (text.startsWith("LLM returned")) return "LLM Error";
  if (text.includes("Decomposed into")) return "Decompose";
  if (text.includes("Decomposition failed")) return "Error";
  return undefined;
}

const categoryConfig: Record<ThoughtCategory, { icon: React.ReactNode; label: string; accent: string; bg: string; border: string }> = {
  system: {
    icon: <Zap className="w-3.5 h-3.5" />,
    label: "System",
    accent: "text-cyan-400",
    bg: "bg-cyan-500/5",
    border: "border-cyan-500/20",
  },
  pm_dispatcher: {
    icon: <Brain className="w-3.5 h-3.5" />,
    label: "PM Dispatcher",
    accent: "text-violet-400",
    bg: "bg-violet-500/5",
    border: "border-violet-500/20",
  },
  agent: {
    icon: <Activity className="w-3.5 h-3.5" />,
    label: "Agent",
    accent: "text-emerald-400",
    bg: "bg-emerald-500/5",
    border: "border-emerald-500/20",
  },
  unknown: {
    icon: <BookOpen className="w-3.5 h-3.5" />,
    label: "Log",
    accent: "text-muted-foreground",
    bg: "bg-muted/5",
    border: "border-border/30",
  },
};

const toolBadgeColors: Record<string, string> = {
  "AST Query": "bg-amber-500/15 text-amber-400 border-amber-500/30",
  "File Read": "bg-blue-500/15 text-blue-400 border-blue-500/30",
  "File Write": "bg-green-500/15 text-green-400 border-green-500/30",
  "Code Edit": "bg-green-500/15 text-green-400 border-green-500/30",
  "Directory Scan": "bg-slate-500/15 text-slate-400 border-slate-500/30",
  "Shell Exec": "bg-orange-500/15 text-orange-400 border-orange-500/30",
  "Memory Read": "bg-purple-500/15 text-purple-400 border-purple-500/30",
  "Memory Write": "bg-purple-500/15 text-purple-400 border-purple-500/30",
  "Complete": "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  "Reasoning": "bg-indigo-500/15 text-indigo-400 border-indigo-500/30",
  "Initialize": "bg-sky-500/15 text-sky-400 border-sky-500/30",
  "LLM Call": "bg-violet-500/15 text-violet-400 border-violet-500/30",
  "LLM Error": "bg-red-500/15 text-red-400 border-red-500/30",
  "Decompose": "bg-teal-500/15 text-teal-400 border-teal-500/30",
  "Error": "bg-red-500/15 text-red-400 border-red-500/30",
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
              }}
            >
              {String(children).replace(/\n$/, "")}
            </SyntaxHighlighter>
          ) : (
            <code className="bg-muted/40 text-[11px] px-1.5 py-0.5 rounded font-mono text-primary/80" {...props}>
              {children}
            </code>
          );
        },
        p({ children }) {
          return <p className="leading-relaxed text-[13px]">{children}</p>;
        },
        strong({ children }) {
          return <strong className="text-foreground font-semibold">{children}</strong>;
        },
        ul({ children }) {
          return <ul className="list-disc list-inside space-y-0.5 text-[13px] pl-1">{children}</ul>;
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

// ─── Single Thought Entry Component ──────────────────────────────────────────

function ThoughtCard({ parsed, index, isLatest }: { parsed: ParsedThought; index: number; isLatest: boolean }) {
  const [collapsed, setCollapsed] = useState(parsed.cleanText.length > 400);
  const config = categoryConfig[parsed.category];
  const toolAction = parsed.toolAction;
  const isLongText = parsed.cleanText.length > 400;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.25, delay: isLatest ? 0.05 : 0, ease: "easeOut" }}
      className={`relative rounded-lg border ${config.border} ${config.bg} overflow-hidden transition-colors duration-200`}
    >
      {/* Header Bar */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border/20">
        <div className="flex items-center gap-2">
          <span className={`${config.accent} flex items-center gap-1`}>
            {config.icon}
            <span className="text-[10px] font-semibold uppercase tracking-wider">{config.label}</span>
          </span>
          {toolAction && (
            <span className={`inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-md border ${toolBadgeColors[toolAction] || "bg-muted/10 text-muted-foreground border-border/30"}`}>
              {toolIcons[toolAction]}
              {toolAction}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {parsed.elapsedMs !== undefined && parsed.elapsedMs > 0 && (
            <span className="text-[10px] text-muted-foreground/60 font-mono">
              {formatElapsed(parsed.elapsedMs)}
            </span>
          )}
          <span className="text-[10px] text-muted-foreground/50 font-mono">
            {new Date(parsed.raw.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </span>
        </div>
      </div>

      {/* Content */}
      <div className="px-3 py-2.5">
        <div className={`prose prose-sm dark:prose-invert max-w-none text-muted-foreground ${isLongText && collapsed ? "max-h-24 overflow-hidden relative" : ""}`}>
          <ThoughtMarkdown content={isLongText && collapsed ? parsed.cleanText.slice(0, 380) + "…" : parsed.cleanText} />
          {isLongText && collapsed && (
            <div className="absolute bottom-0 left-0 right-0 h-10 bg-gradient-to-t from-background/80 to-transparent" />
          )}
        </div>
        {isLongText && (
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="flex items-center gap-1 mt-1.5 text-[11px] text-primary/70 hover:text-primary transition-colors"
          >
            {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            {collapsed ? "Show full reasoning" : "Collapse"}
          </button>
        )}
      </div>

      {/* Step indicator line */}
      <div className={`absolute left-0 top-0 bottom-0 w-0.5 ${config.accent.replace("text-", "bg-")} opacity-40`} />
    </motion.div>
  );
}

// ─── Thought Stream Content (shared between drawer and modal) ────────────────

function ThoughtStream({
  thoughts,
  isLoading,
  error,
  taskId,
  taskStatus,
  searchTerm,
}: {
  thoughts: ParsedThought[];
  isLoading: boolean;
  error: unknown;
  taskId: string | null;
  taskStatus?: string;
  searchTerm: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevCountRef = useRef(0);

  const filtered = useMemo(() => {
    if (!searchTerm) return thoughts;
    const lower = searchTerm.toLowerCase();
    return thoughts.filter(
      (t) =>
        t.cleanText.toLowerCase().includes(lower) ||
        (t.toolAction || "").toLowerCase().includes(lower) ||
        t.category.toLowerCase().includes(lower)
    );
  }, [thoughts, searchTerm]);

  useEffect(() => {
    if (scrollRef.current && filtered.length > prevCountRef.current) {
      const el = scrollRef.current;
      setTimeout(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      }, 80);
    }
    prevCountRef.current = filtered.length;
  }, [filtered.length]);

  const isActive = taskStatus === "running";

  return (
    <div className="flex-1 overflow-hidden relative">
      <ScrollArea className="h-full" ref={scrollRef}>
        <div className="p-4 space-y-2.5">
          {isLoading ? (
            <div className="flex flex-col items-center justify-center h-40 text-muted-foreground gap-3">
              <Loader2 className="w-5 h-5 animate-spin text-primary/60" />
              <p className="text-xs">Syncing reasoning stream…</p>
            </div>
          ) : error ? (
            <div className="text-destructive text-center text-sm mt-10 bg-destructive/10 p-4 rounded-lg border border-destructive/20">
              Failed to fetch agent thoughts.
            </div>
          ) : filtered.length > 0 ? (
            <AnimatePresence mode="popLayout">
              {filtered.map((entry, index) => (
                <ThoughtCard
                  key={`${entry.raw.ts}-${index}`}
                  parsed={entry}
                  index={index}
                  isLatest={index === filtered.length - 1}
                />
              ))}
            </AnimatePresence>
          ) : (
            <div className="flex flex-col items-center justify-center h-40 text-muted-foreground gap-3">
              <Brain className="w-10 h-10 text-muted-foreground/15" />
              <p className="text-xs">
                {searchTerm ? "No matching thoughts found." : "No reasoning steps recorded yet."}
              </p>
            </div>
          )}
        </div>
      </ScrollArea>

      {/* Live Indicator */}
      {isActive && filtered.length > 0 && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2">
          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-background/90 border border-border/40 backdrop-blur-sm shadow-lg">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
            </span>
            <span className="text-[10px] text-muted-foreground font-medium">Live — polling every 3s</span>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Header Stats Bar ────────────────────────────────────────────────────────

function StatsBar({
  totalSteps,
  totalElapsedMs,
  searchTerm,
  onSearchChange,
  isFullPage,
  onToggleMode,
}: {
  totalSteps: number;
  totalElapsedMs: number;
  searchTerm: string;
  onSearchChange: (v: string) => void;
  isFullPage: boolean;
  onToggleMode: () => void;
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border/30 bg-muted/10">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <Clock className="w-3.5 h-3.5" />
        <span className="text-[11px] font-mono">{formatTotalElapsed(totalElapsedMs)}</span>
      </div>
      <div className="w-px h-4 bg-border/40" />
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <Activity className="w-3.5 h-3.5" />
        <span className="text-[11px] font-mono">{totalSteps} steps</span>
      </div>
      <div className="flex-1" />
      <div className="relative">
        <Search className="w-3 h-3 text-muted-foreground/50 absolute left-2 top-1/2 -translate-y-1/2" />
        <input
          type="text"
          value={searchTerm}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="Filter…"
          className="w-28 focus:w-44 transition-all text-[11px] bg-muted/20 border border-border/30 rounded-md pl-7 pr-2 py-1 text-foreground placeholder:text-muted-foreground/40 focus:outline-none focus:ring-1 focus:ring-primary/40"
        />
      </div>
      <button
        onClick={onToggleMode}
        className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
        title={isFullPage ? "Switch to drawer" : "Switch to full page"}
      >
        {isFullPage ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
      </button>
    </div>
  );
}

// ─── Main Component ──────────────────────────────────────────────────────────

export function AgentThoughtDrawer({ taskId, taskTitle, taskStatus, isOpen, onOpenChange }: AgentThoughtDrawerProps) {
  const [isFullPage, setIsFullPage] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");

  const { data, isLoading, error } = useQuery<{ thoughts: ThoughtEntry[] }>({
    queryKey: ["task-thoughts", taskId],
    queryFn: async () => {
      if (!taskId) return { thoughts: [] };
      const res = await fetch(`/api/tasks/${taskId}/thoughts`);
      if (!res.ok) throw new Error("Failed to fetch thoughts");
      return res.json();
    },
    enabled: !!taskId && isOpen,
    refetchInterval: 3000,
  });

  // Parse and enrich thought entries
  const parsedThoughts: ParsedThought[] = useMemo(() => {
    const raw = data?.thoughts || [];
    return raw.map((entry, index) => {
      const { category, cleanText } = parseCategory(entry.thought);
      const toolAction = extractToolAction(cleanText);
      const elapsedMs =
        index > 0
          ? new Date(entry.ts).getTime() - new Date(raw[index - 1].ts).getTime()
          : undefined;
      return { raw: entry, category, cleanText, toolAction, elapsedMs };
    });
  }, [data?.thoughts]);

  const totalElapsedMs = useMemo(() => {
    if (parsedThoughts.length < 2) return 0;
    return (
      new Date(parsedThoughts[parsedThoughts.length - 1].raw.ts).getTime() -
      new Date(parsedThoughts[0].raw.ts).getTime()
    );
  }, [parsedThoughts]);

  // Reset state when closing
  useEffect(() => {
    if (!isOpen) {
      setSearchTerm("");
    }
  }, [isOpen]);

  const headerContent = (
    <>
      <div className="flex items-center gap-2 text-foreground">
        <div className="p-1.5 rounded-md bg-primary/10">
          <Brain className="w-4 h-4 text-primary" />
        </div>
        <div>
          <h3 className="text-sm font-semibold">Agent Reasoning</h3>
          {taskTitle && (
            <p className="text-[11px] text-muted-foreground truncate max-w-[300px]">{taskTitle}</p>
          )}
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground mt-1">
        Live view of the LLM's internal reasoning for task <code className="text-[10px] bg-muted/30 px-1 py-0.5 rounded font-mono">{taskId}</code>
      </p>
    </>
  );

  const sharedContent = (
    <>
      <StatsBar
        totalSteps={parsedThoughts.length}
        totalElapsedMs={totalElapsedMs}
        searchTerm={searchTerm}
        onSearchChange={setSearchTerm}
        isFullPage={isFullPage}
        onToggleMode={() => setIsFullPage(!isFullPage)}
      />
      <ThoughtStream
        thoughts={parsedThoughts}
        isLoading={isLoading}
        error={error}
        taskId={taskId}
        taskStatus={taskStatus}
        searchTerm={searchTerm}
      />
    </>
  );

  // ─── Full-Page Modal ─────────────────────────────────────────────────────
  if (isFullPage) {
    return (
      <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-[90vw] w-[90vw] h-[85vh] flex flex-col p-0 gap-0 overflow-hidden border-border/40 bg-background/95 backdrop-blur-xl">
          <DialogHeader className="p-5 border-b border-border/30 shrink-0">
            <DialogTitle asChild>{headerContent}</DialogTitle>
            <DialogDescription className="sr-only">Live agent reasoning and thought process viewer</DialogDescription>
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
        className="w-[90vw] sm:max-w-[560px] flex flex-col p-0 h-full border-l border-border/30 shadow-2xl bg-background/95 backdrop-blur-xl"
      >
        <SheetHeader className="p-5 border-b border-border/30 shrink-0">
          <SheetTitle asChild>{headerContent}</SheetTitle>
          <SheetDescription className="sr-only">Live agent reasoning and thought process viewer</SheetDescription>
        </SheetHeader>
        <div className="flex-1 flex flex-col min-h-0">
          {sharedContent}
        </div>
      </SheetContent>
    </Sheet>
  );
}
