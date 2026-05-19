import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import "./index.css";
import { flushLogs, createLogger } from "@/utils/logger";

const log = createLogger('main');

// Flush buffered LogLoom entries before the page unloads to prevent data loss.
window.addEventListener('beforeunload', flushLogs);

log.info('bootstrap', 'Flume Dashboard initializing');
createRoot(document.getElementById("root")!).render(<App />);
