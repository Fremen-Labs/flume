import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import {
  LayoutDashboard, FolderKanban, ListTodo, Bot, Activity, BarChart3,
  Settings, ChevronLeft, ChevronRight, Zap, Radar, Sun, Moon
} from 'lucide-react';
import { SidebarNavItem } from '@/components/SidebarNavItem';
import { MeshBackground } from '@/components/MeshBackground';
import { SnapshotErrorBanner } from '@/components/SnapshotErrorBanner';
import { motion, AnimatePresence } from 'framer-motion';
import { useTheme } from '@/hooks/useTheme';

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Overview' },
  { to: '/mission-control', icon: Radar, label: 'Mission Control' },
  { to: '/projects', icon: FolderKanban, label: 'Projects' },
  { to: '/queue', icon: ListTodo, label: 'Work Queue' },
  { to: '/agents', icon: Bot, label: 'Agents' },
  { to: '/activity', icon: Activity, label: 'Activity' },
  { to: '/analytics', icon: BarChart3, label: 'Analytics' },
];

export function AppLayout() {
  const [collapsed, setCollapsed] = useState(false);
  const { theme, toggleTheme } = useTheme();

  return (
    <div className="flex min-h-screen w-full">
      <MeshBackground />

      {/* Sidebar */}
      <motion.aside
        initial={false}
        animate={{ width: collapsed ? 64 : 240 }}
        transition={{ duration: 0.2, ease: 'easeInOut' }}
        className="relative z-20 flex flex-col glass-sidebar h-screen sticky top-0"
      >
        {/* Top reflection edge */}
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-primary/20 to-transparent" />

        {/* Logo */}
        <div className="flex items-center gap-2.5 px-4 h-16 border-b border-border">
          <div className="w-8 h-8 rounded-lg bg-primary/15 flex items-center justify-center flex-shrink-0 breathing">
            <Zap className="w-4 h-4 text-primary icon-glow-active" />
          </div>
          <AnimatePresence>
            {!collapsed && (
              <motion.span
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: 'auto' }}
                exit={{ opacity: 0, width: 0 }}
                className="text-sm font-bold tracking-tight text-foreground overflow-hidden whitespace-nowrap"
              >
                Flume
              </motion.span>
            )}
          </AnimatePresence>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-2 py-4 space-y-1 overflow-y-auto">
          {navItems.map((item) => (
            <SidebarNavItem key={item.to} {...item} collapsed={collapsed} />
          ))}
        </nav>

        {/* Bottom - always visible */}
        <div className="px-2 py-3 border-t border-border space-y-1 flex-shrink-0">
          <button
            onClick={toggleTheme}
            className="flex items-center gap-2.5 w-full px-3 py-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent/50 transition-colors"
          >
            {theme === 'dark' ? <Sun className="w-4 h-4 flex-shrink-0" /> : <Moon className="w-4 h-4 flex-shrink-0" />}
            <AnimatePresence>
              {!collapsed && (
                <motion.span
                  initial={{ opacity: 0, width: 0 }}
                  animate={{ opacity: 1, width: 'auto' }}
                  exit={{ opacity: 0, width: 0 }}
                  className="text-sm overflow-hidden whitespace-nowrap"
                >
                  {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
                </motion.span>
              )}
            </AnimatePresence>
          </button>
          <SidebarNavItem to="/settings" icon={Settings} label="Settings" collapsed={collapsed} />
        </div>

        {/* Collapse button */}
        <button
          onClick={() => setCollapsed(!collapsed)}
          className="absolute -right-3 top-20 w-6 h-6 rounded-full bg-card/80 backdrop-blur-md border border-white/10 flex items-center justify-center text-muted-foreground hover:text-foreground hover:border-primary/30 transition-all z-30"
        >
          {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronLeft className="w-3 h-3" />}
        </button>
      </motion.aside>

      {/* Main */}
      <main className="flex-1 relative z-10 overflow-auto flex flex-col">
        <SnapshotErrorBanner />
        <div className="flex-1">
          <Outlet />
        </div>
        <footer className="px-6 py-3 border-t border-border text-xs text-muted-foreground flex items-center justify-center">
          <span>Project communication for service businesses that care about their client's experience.</span>
        </footer>
      </main>
    </div>
  );
}