import React, { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import ReactMarkdown from "react-markdown";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Loader2, Brain } from "lucide-react";

interface AgentThoughtDrawerProps {
  taskId: string | null;
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
}

interface ThoughtEntry {
  ts: string;
  thought: string;
}

export function AgentThoughtDrawer({ taskId, isOpen, onOpenChange }: AgentThoughtDrawerProps) {
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

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) {
      const el = scrollRef.current;
      // Use setTimeout to ensure DOM has updated before scrolling
      setTimeout(() => {
        el.scrollTop = el.scrollHeight;
      }, 50);
    }
  }, [data?.thoughts]);

  return (
    <Sheet open={isOpen} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[90vw] sm:max-w-[600px] flex flex-col p-0 h-full border-l border-border/50 shadow-2xl">
        <SheetHeader className="p-6 border-b border-border/50 bg-background/95 backdrop-blur shrink-0">
          <SheetTitle className="flex items-center gap-2 text-foreground">
            <Brain className="w-5 h-5 text-primary" />
            Agent Thought Process
          </SheetTitle>
          <SheetDescription className="text-xs">
            Live view of the LLM's internal reasoning for task {taskId}.
          </SheetDescription>
        </SheetHeader>
        
        <div className="flex-1 overflow-hidden relative bg-muted/20">
          <ScrollArea className="h-full" ref={scrollRef}>
            <div className="p-6">
              {isLoading ? (
                <div className="flex flex-col items-center justify-center h-40 text-muted-foreground gap-3">
                  <Loader2 className="w-5 h-5 animate-spin text-primary/60" />
                  <p className="text-xs">Buffering thoughts...</p>
                </div>
              ) : error ? (
                <div className="text-destructive text-center text-sm mt-10 bg-destructive/10 p-4 rounded-lg border border-destructive/20">
                  Error fetching thoughts.
                </div>
              ) : data?.thoughts && data.thoughts.length > 0 ? (
                <div className="flex flex-col gap-6 line-numbers-mode">
                  {data.thoughts.map((entry, index) => (
                    <div key={index} className="flex flex-col gap-2 border-b border-border/40 pb-5 last:border-0 last:pb-0">
                      <div className="flex items-center gap-2">
                        <span className="w-1.5 h-1.5 rounded-full bg-primary/60" />
                        <span className="text-[10px] text-muted-foreground font-mono uppercase tracking-wider">
                          {new Date(entry.ts).toLocaleTimeString()}
                        </span>
                      </div>
                      <div className="prose prose-sm dark:prose-invert max-w-none text-muted-foreground pl-3 border-l-2 border-border/30">
                        <ReactMarkdown>{entry.thought}</ReactMarkdown>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center h-40 text-muted-foreground gap-2">
                  <Brain className="w-8 h-8 text-muted-foreground/20" />
                  <p className="text-xs">No reasoning steps recorded yet.</p>
                </div>
              )}
            </div>
          </ScrollArea>
        </div>
      </SheetContent>
    </Sheet>
  );
}
