import { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Sparkles, KeyRound, DollarSign, Wallet, Plus, Loader2 } from 'lucide-react';
import type { FrontierModelWeight, FrontierProviderCatalog, LlmCredentialSummary } from '@/types';

interface Props {
  catalog: FrontierProviderCatalog[];
  onClose: () => void;
  onAdd: (model: FrontierModelWeight) => void;
}

const PROVIDER_VISUALS: Record<string, { brandColor: string; bgSoft: string; borderActive: string; icon: string }> = {
  openai:    { brandColor: 'bg-emerald-500', bgSoft: 'bg-emerald-500/10 text-emerald-400', borderActive: 'border-emerald-500/60 ring-1 ring-emerald-500/40', icon: '🟢' },
  anthropic: { brandColor: 'bg-amber-500',   bgSoft: 'bg-amber-500/10 text-amber-400',   borderActive: 'border-amber-500/60 ring-1 ring-amber-500/40', icon: '🟠' },
  gemini:    { brandColor: 'bg-blue-500',    bgSoft: 'bg-blue-500/10 text-blue-400',     borderActive: 'border-blue-500/60 ring-1 ring-blue-500/40', icon: '🔵' },
  xai:       { brandColor: 'bg-violet-500',  bgSoft: 'bg-violet-500/10 text-violet-400', borderActive: 'border-violet-500/60 ring-1 ring-violet-500/40', icon: '🟣' },
};

export function AddFrontierModelModal({ catalog, onClose, onAdd }: Props) {
  const [providerId, setProviderId] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [credentialId, setCredentialId] = useState<string | null>(null);
  const [isNewCredential, setIsNewCredential] = useState(false);
  const [newApiKey, setNewApiKey] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [budget, setBudget] = useState(50);
  const [isHoveringBudget, setIsHoveringBudget] = useState(false);

  const selectedProvider = catalog.find(p => p.id === providerId);
  const models = selectedProvider?.models ?? [];
  const credentials = selectedProvider?.credentials ?? [];
  
  const v = providerId ? PROVIDER_VISUALS[providerId] : null;

  // Auto-select credential if there's exactly one, or clear model if provider changes
  useEffect(() => {
    setModel(null);
    setIsNewCredential(credentials.length === 0);
    if (credentials.length === 1) {
      setCredentialId(credentials[0].id);
      setIsNewCredential(false);
    } else {
      setCredentialId(null);
    }
  }, [providerId, credentials.length]);

  const handleAdd = async () => {
    if (!providerId || !model) return;
    
    let finalCredId = credentialId ?? '';
    setIsSubmitting(true);
    
    try {
      if (isNewCredential && newApiKey) {
        const res = await fetch('/api/settings/llm/credentials', {
           method: 'POST',
           headers: { 'Content-Type': 'application/json' },
           body: JSON.stringify({
             action: 'upsert',
             provider: providerId,
             label: `${selectedProvider?.label || providerId} Key`,
             apiKey: newApiKey,
           }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to save credential');
        if (data.credential_id) {
          finalCredId = data.credential_id;
        }
      }
      
      onAdd({
        provider: providerId,
        model,
        credential_id: finalCredId,
        weight: 0.5,
        budget_usd: budget,
        spent_usd: 0,
        circuit_open: false,
      });
      onClose();
    } catch (err) {
      console.error(err);
      setIsSubmitting(false);
    }
  };

  const isReady = providerId != null && model != null && (isNewCredential ? newApiKey.length > 5 : credentialId != null);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Heavy netflix-grade backdrop fade */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="absolute inset-0 bg-black/80 backdrop-blur-md"
        onClick={onClose}
      />

      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        transition={{ type: 'spring', damping: 25, stiffness: 300 }}
        className="relative w-full max-w-2xl mx-4 overflow-hidden rounded-2xl border border-white/10 bg-slate-950/80 shadow-2xl shadow-indigo-500/10"
      >
        {/* Ambient Top Glow Based on Active Provider */}
        <AnimatePresence mode="wait">
          {providerId && v && (
            <motion.div
              key={providerId}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className={`absolute top-0 inset-x-0 h-32 ${v.brandColor} blur-[120px] opacity-20 pointer-events-none`}
            />
          )}
        </AnimatePresence>

        <div className="relative p-8 space-y-8 max-h-[85vh] overflow-y-auto no-scrollbar">
          {/* Header */}
          <div className="flex items-start justify-between">
            <div>
              <h2 className="text-2xl font-bold tracking-tight text-white flex items-center gap-2">
                <Sparkles className="w-6 h-6 text-indigo-400" />
                Add Frontier Intelligence
              </h2>
              <p className="text-sm text-slate-400 mt-1">
                Configure cloud models for intelligent mesh routing.
              </p>
            </div>
            <button
              onClick={onClose}
              className="p-2 rounded-full hover:bg-white/10 text-slate-400 hover:text-white transition-colors"
            >
              <X className="w-5 h-5" />
            </button>
          </div>

          <div className="space-y-10">
            {/* STAGE 1: Provider Selection */}
            <div className="space-y-4">
              <div className="flex items-center gap-3">
                <div className="flex items-center justify-center w-6 h-6 rounded-full bg-white/10 text-xs font-bold text-white">1</div>
                <h3 className="text-lg font-medium text-slate-200">Select Provider</h3>
              </div>
              
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                {catalog.map(p => {
                  const isActive = providerId === p.id;
                  const visual = PROVIDER_VISUALS[p.id] ?? PROVIDER_VISUALS.openai;
                  return (
                    <motion.button
                      key={p.id}
                      onClick={() => setProviderId(p.id)}
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                      className={`relative flex flex-col items-center justify-center p-4 rounded-xl border transition-all duration-300 ${
                        isActive
                          ? `${visual.bgSoft} ${visual.borderActive}`
                          : 'bg-white/5 border-white/10 hover:bg-white/10 text-slate-300'
                      }`}
                    >
                      <span className="text-2xl mb-2">{visual.icon}</span>
                      <span className={`text-sm font-semibold ${isActive ? '' : 'opacity-80'}`}>{p.label}</span>
                    </motion.button>
                  );
                })}
              </div>
            </div>

            {/* STAGE 2: Model Selection (Animated In) */}
            <AnimatePresence>
              {providerId && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="space-y-4 overflow-hidden"
                >
                  <div className="flex items-center gap-3 pt-2">
                    <div className="flex items-center justify-center w-6 h-6 rounded-full bg-white/10 text-xs font-bold text-white">2</div>
                    <h3 className="text-lg font-medium text-slate-200">Select Model</h3>
                  </div>
                  
                  <div className="flex flex-wrap gap-2">
                    {models.map(m => {
                      const isActive = model === m;
                      return (
                        <motion.button
                          key={m}
                          onClick={() => setModel(m)}
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                          className={`px-4 py-2 rounded-full text-sm font-medium transition-all duration-300 ${
                            isActive
                              ? `${v?.brandColor} text-white shadow-lg`
                              : 'bg-white/5 border border-white/10 text-slate-300 hover:bg-white/10 hover:border-white/20'
                          }`}
                        >
                          {m}
                        </motion.button>
                      );
                    })}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* STAGE 3: Config & Budget */}
            <AnimatePresence>
              {model && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="space-y-6 overflow-hidden"
                >
                  <div className="flex items-center gap-3 pt-2">
                    <div className="flex items-center justify-center w-6 h-6 rounded-full bg-white/10 text-xs font-bold text-white">3</div>
                    <h3 className="text-lg font-medium text-slate-200">Configure Limits</h3>
                  </div>
                  
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    {/* API Credential */}
                    <div className="space-y-3">
                      <label className="text-sm text-slate-400 flex items-center gap-2">
                        <KeyRound className="w-4 h-4" /> API Credential
                      </label>
                      <div className="space-y-2">
                        {credentials.map(c => {
                          const isActive = !isNewCredential && credentialId === c.id;
                          return (
                            <button
                              key={c.id}
                              onClick={() => {
                                setCredentialId(c.id);
                                setIsNewCredential(false);
                              }}
                              className={`w-full flex items-center justify-between px-4 py-3 rounded-xl border text-left transition-all ${
                                isActive
                                  ? 'bg-indigo-500/10 border-indigo-500/30 text-indigo-300'
                                  : 'bg-white/5 border-white/10 hover:bg-white/10 text-slate-300'
                              }`}
                            >
                              <span className="text-sm font-medium">{c.label}</span>
                              {c.has_key && <span className="text-[10px] uppercase font-bold text-emerald-400 bg-emerald-400/10 px-2 py-0.5 rounded">Active Key</span>}
                            </button>
                          );
                        })}

                        <button
                          onClick={() => {
                            setCredentialId(null);
                            setIsNewCredential(true);
                          }}
                          className={`w-full flex items-center justify-center px-4 py-3 rounded-xl border border-dashed transition-all ${
                            isNewCredential
                              ? 'bg-white/10 border-white/40 text-white'
                              : 'bg-transparent border-white/20 hover:border-white/40 text-slate-400 hover:text-white'
                          }`}
                        >
                          <Plus className="w-4 h-4 mr-2" /> Add New API Key
                        </button>
                        
                        <AnimatePresence>
                          {isNewCredential && (
                            <motion.div
                              initial={{ opacity: 0, height: 0 }}
                              animate={{ opacity: 1, height: 'auto' }}
                              exit={{ opacity: 0, height: 0 }}
                              className="overflow-hidden pt-2"
                            >
                              <input
                                type="text"
                                placeholder={`Enter ${selectedProvider?.label || providerId} API Key...`}
                                value={newApiKey}
                                onChange={(e) => setNewApiKey(e.target.value)}
                                className="w-full bg-slate-900/50 border border-white/10 rounded-lg px-4 py-3 text-sm text-white focus:outline-none focus:ring-1 focus:ring-indigo-500/50 placeholder:text-slate-600 font-mono"
                              />
                            </motion.div>
                          )}
                        </AnimatePresence>
                      </div>
                    </div>

                    {/* Netflix-style massive budget slider */}
                    <div className="space-y-3">
                      <label className="text-sm text-slate-400 flex items-center gap-2">
                        <Wallet className="w-4 h-4" /> Monthly Spend Cap
                      </label>
                      <div 
                        className="glass-card p-6 border-white/10 flex flex-col items-center justify-center relative overflow-hidden group"
                        onMouseEnter={() => setIsHoveringBudget(true)}
                        onMouseLeave={() => setIsHoveringBudget(false)}
                      >
                        {/* Interactive dynamic background based on budget */}
                        <div 
                          className="absolute bottom-0 left-0 right-0 bg-red-500/10 transition-all duration-300"
                          style={{ height: `${(budget / 500) * 100}%` }}
                        />

                        <div className="flex items-baseline gap-1 relative z-10">
                          <DollarSign className="w-6 h-6 text-white/50" />
                          <span className="text-5xl font-light text-white tracking-tight tabular-nums">
                            {budget}
                          </span>
                          <span className="text-white/40 ml-1">.00</span>
                        </div>
                        
                        <input
                          type="range"
                          min="5"
                          max="500"
                          step="5"
                          value={budget}
                          onChange={(e) => setBudget(Number(e.target.value))}
                          className="w-full mt-6 h-2 rounded-full appearance-none bg-white/10 accent-indigo-500 cursor-pointer relative z-10"
                        />
                        
                        <div className="w-full flex justify-between text-[10px] text-white/40 font-mono mt-2 relative z-10">
                          <span>$5</span>
                          <span>$500</span>
                        </div>
                      </div>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>

        {/* Action Footer */}
        <div className="p-6 bg-white/5 border-t border-white/10">
          <motion.button
            whileHover={isReady && !isSubmitting ? { scale: 1.01 } : {}}
            whileTap={isReady && !isSubmitting ? { scale: 0.99 } : {}}
            onClick={handleAdd}
            disabled={!isReady || isSubmitting}
            className={`w-full py-4 rounded-xl text-base font-semibold transition-all duration-300 flex items-center justify-center gap-2 ${
              isReady && !isSubmitting
                ? 'bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-500/25'
                : 'bg-white/5 text-white/30 cursor-not-allowed'
            }`}
          >
            {isSubmitting ? (
              <>
                <Loader2 className="w-5 h-5 animate-spin" />
                Saving Configuration...
              </>
            ) : isReady ? (
              <>
                <Sparkles className="w-5 h-5" />
                Add {model} to Routing Engine
              </>
            ) : (
              'Complete Configuration'
            )}
          </motion.button>
        </div>
      </motion.div>
    </div>
  );
}
