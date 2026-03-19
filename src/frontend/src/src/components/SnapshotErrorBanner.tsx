import { AlertTriangle } from 'lucide-react';
import { useSnapshot } from '@/hooks/useSnapshot';

export function SnapshotErrorBanner() {
  const { error } = useSnapshot();
  if (!error) return null;

  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="bg-destructive/15 border-b border-destructive/30 px-4 py-3 flex items-center gap-3 text-destructive">
      <AlertTriangle className="w-5 h-5 flex-shrink-0" />
      <p className="text-sm font-medium flex-1">{msg}</p>
    </div>
  );
}
