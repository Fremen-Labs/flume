import { useState } from 'react';
import { motion } from 'framer-motion';
import { Loader2, Send, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { createLogger } from '@/utils/logger';

const log = createLogger('pages.GatewayChatPage');

interface ChatTurn {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

interface ChatResponse {
  message?: { content?: string };
  error?: string;
}

export default function GatewayChatPage() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  async function send() {
    const content = draft.trim();
    if (!content || busy) return;
    const next: ChatTurn[] = [...turns, { role: 'user', content }];
    setTurns(next);
    setDraft('');
    setBusy(true);
    try {
      const res = await fetch('/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: next
            .filter((t) => t.role !== 'system')
            .map((t) => ({ role: t.role, content: t.content })),
        }),
      });
      const body = (await res.json().catch(() => ({}))) as ChatResponse;
      if (!res.ok) {
        const err = body.error || `HTTP ${res.status}`;
        log.error('send', err, { status: res.status });
        setTurns([...next, { role: 'system', content: err }]);
        return;
      }
      setTurns([...next, { role: 'assistant', content: body.message?.content || '(empty reply)' }]);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      log.error('send', msg);
      setTurns([...next, { role: 'system', content: msg }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-5 lg:p-6 max-w-3xl mx-auto flex flex-col h-[calc(100vh-6rem)]">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} className="mb-4">
        <h1 className="text-2xl font-semibold tracking-tight">Chat</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Sends <code className="text-xs">POST /v1/chat</code> to the gateway. Requires a reachable Ollama node or frontier provider.
        </p>
      </motion.div>

      <div className="flex-1 overflow-y-auto space-y-3 mb-4">
        {turns.length === 0 && (
          <p className="text-sm text-muted-foreground">No messages yet.</p>
        )}
        {turns.map((t, i) => (
          <div
            key={`${t.role}-${i}`}
            className={`rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
              t.role === 'user'
                ? 'bg-primary/15 ml-8'
                : t.role === 'assistant'
                  ? 'bg-card mr-8 border border-border'
                  : 'bg-destructive/10 text-destructive'
            }`}
          >
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">{t.role}</div>
            {t.content}
          </div>
        ))}
      </div>

      <div className="flex items-end gap-2">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder="Message the gateway…"
          className="min-h-[3rem]"
          disabled={busy}
        />
        <Button onClick={() => void send()} disabled={busy || !draft.trim()} size="icon" aria-label="Send">
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Clear"
          onClick={() => setTurns([])}
          disabled={busy || turns.length === 0}
        >
          <Trash2 className="w-4 h-4" />
        </Button>
      </div>
    </div>
  );
}
