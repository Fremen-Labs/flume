/** True when the SPA is built for the core stack (gateway + ES + OpenBao, no workers). */
export const isCoreUI = import.meta.env.VITE_CORE_UI === 'true';
