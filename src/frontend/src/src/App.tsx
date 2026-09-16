import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeProvider } from "@/hooks/useTheme";
import { AppLayout } from "@/components/AppLayout";
import { isCoreUI } from "@/lib/core";
import Dashboard from "@/pages/Dashboard";
import CoreOverview from "@/pages/CoreOverview";
import GatewayChatPage from "@/pages/GatewayChatPage";
import CoreSettingsPage from "@/pages/CoreSettingsPage";
import ProjectsPage from "@/pages/ProjectsPage";
import ProjectDetailPage from "@/pages/ProjectDetailPage";
import QueuePage from "@/pages/QueuePage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import SettingsPage from "@/pages/SettingsPage";
import SecurityPage from "@/pages/SecurityPage";
import MissionControlPage from "@/pages/MissionControlPage";
import NodesOverview from "@/pages/NodesOverview";
import NotFound from "./pages/NotFound.tsx";

const queryClient = new QueryClient();

const coreRoutes = (
  <Route element={<AppLayout />}>
    <Route path="/" element={<CoreOverview />} />
    <Route path="/nodes" element={<NodesOverview />} />
    <Route path="/chat" element={<GatewayChatPage />} />
    <Route path="/settings" element={<CoreSettingsPage />} />
  </Route>
);

const fullRoutes = (
  <Route element={<AppLayout />}>
    <Route path="/" element={<Dashboard />} />
    <Route path="/mission-control" element={<MissionControlPage />} />
    <Route path="/projects" element={<ProjectsPage />} />
    <Route path="/projects/:id" element={<ProjectDetailPage />} />
    <Route path="/queue" element={<QueuePage />} />
    <Route path="/analytics" element={<AnalyticsPage />} />
    <Route path="/security" element={<SecurityPage />} />
    <Route path="/nodes" element={<NodesOverview />} />
    <Route path="/settings" element={<SettingsPage />} />
  </Route>
);

const App = () => (
  <QueryClientProvider client={queryClient}>
    <ThemeProvider>
      <TooltipProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter>
          <Routes>
            {isCoreUI ? coreRoutes : fullRoutes}
            <Route path="*" element={<NotFound />} />
          </Routes>
        </BrowserRouter>
      </TooltipProvider>
    </ThemeProvider>
  </QueryClientProvider>
);

export default App;
