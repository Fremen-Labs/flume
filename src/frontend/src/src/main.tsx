import { createRoot } from "react-dom/client";
import "./index.css";
import { flushLogs, createLogger } from "@/utils/logger";

const log = createLogger('main');

window.addEventListener('beforeunload', flushLogs);

async function boot() {
  const mod = import.meta.env.VITE_CORE_UI === "true"
    ? await import("./App.core")
    : await import("./App");
  log.info('bootstrap', import.meta.env.VITE_CORE_UI === "true" ? 'Flume core console initializing' : 'Flume Dashboard initializing');
  createRoot(document.getElementById("root")!).render(<mod.default />);
}

void boot();
