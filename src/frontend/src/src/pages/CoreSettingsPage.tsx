import { useQuery } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { Moon, Palette, Sun } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import type { StackStatus } from '@/pages/CoreOverview';

async function fetchStack(): Promise<StackStatus> {
  const res = await fetch('/api/stack');
  if (!res.ok) throw new Error(`Stack status failed: ${res.status}`);
  return res.json();
}

export default function CoreSettingsPage() {
  const { theme, toggleTheme, skin, setSkin } = useTheme();
  const { data } = useQuery({
    queryKey: ['stack'],
    queryFn: fetchStack,
    refetchInterval: 15_000,
  });

  return (
    <div className="p-5 lg:p-6 max-w-2xl mx-auto space-y-8">
      <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }}>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Appearance for this console. LLM credentials and routing live on the gateway via OpenBao and Elasticsearch.
        </p>
      </motion.div>

      <section className="glass-card p-5 space-y-4">
        <div className="flex items-center gap-2">
          <Palette className="w-4 h-4 text-primary" />
          <h2 className="text-sm font-semibold">Appearance</h2>
        </div>
        <div className="flex items-center justify-between">
          <Label>Theme</Label>
          <Button variant="outline" size="sm" onClick={toggleTheme}>
            {theme === 'dark' ? <Sun className="w-4 h-4 mr-2" /> : <Moon className="w-4 h-4 mr-2" />}
            {theme === 'dark' ? 'Light' : 'Dark'}
          </Button>
        </div>
        <div className="flex items-center justify-between">
          <Label>Skin</Label>
          <Button variant="outline" size="sm" onClick={() => setSkin(skin === 'default' ? 'retro' : 'default')}>
            {skin === 'default' ? 'Retro' : 'Default'}
          </Button>
        </div>
      </section>

      <section className="glass-card p-5 space-y-3 text-sm">
        <h2 className="text-sm font-semibold">Connections</h2>
        <p className="text-muted-foreground">
          Gateway <span className="text-foreground">{data?.gateway?.status ?? '…'}</span>
          {' · '}Elasticsearch <span className="text-foreground">{data?.elasticsearch?.status ?? '…'}</span>
          {' · '}OpenBao <span className="text-foreground">{data?.openbao?.status ?? '…'}</span>
        </p>
        <p className="text-xs text-muted-foreground">
          Add inference nodes on the Nodes page. Frontier keys are stored in OpenBao; the gateway reads them at request time.
        </p>
      </section>
    </div>
  );
}
