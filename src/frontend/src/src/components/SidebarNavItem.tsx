import { NavLink as RouterNavLink, useLocation } from 'react-router-dom';
import { LucideIcon } from 'lucide-react';

interface SidebarNavItemProps {
  to: string;
  icon: LucideIcon;
  label: string;
  collapsed?: boolean;
}

export function SidebarNavItem({ to, icon: Icon, label, collapsed }: SidebarNavItemProps) {
  const location = useLocation();
  const isActive = location.pathname === to || (to !== '/' && location.pathname.startsWith(to));

  return (
    <RouterNavLink
      to={to}
      end={to === '/'}
      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 group relative overflow-hidden
        ${isActive
          ? 'bg-primary/10 text-primary'
          : 'text-muted-foreground hover:text-foreground hover:bg-white/[0.03]'
        }`}
    >
      {/* Active glow background */}
      {isActive && (
        <div className="absolute inset-0 bg-gradient-to-r from-primary/10 via-primary/5 to-transparent pointer-events-none" />
      )}
      <Icon className={`w-[18px] h-[18px] flex-shrink-0 relative z-10 ${isActive ? 'text-primary icon-glow-active' : ''}`} />
      {!collapsed && <span className="truncate relative z-10">{label}</span>}
      {isActive && !collapsed && <div className="ml-auto w-1 h-4 rounded-full bg-primary relative z-10" />}
      {isActive && <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[2px] h-5 bg-primary rounded-r-full" />}
    </RouterNavLink>
  );
}