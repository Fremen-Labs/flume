import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export type Theme = 'dark';
export type Skin = 'default' | 'retro';

interface ThemeContextType {
  theme: Theme;
  skin: Skin;
  toggleTheme: () => void;
  setSkin: (skin: Skin) => void;
}

const ThemeContext = createContext<ThemeContextType>({
  theme: 'dark',
  skin: 'default',
  toggleTheme: () => {},
  setSkin: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [skin, setSkinState] = useState<Skin>(() => {
    const stored = localStorage.getItem('skin');
    return (stored === 'retro' || stored === 'default') ? stored : 'default';
  });

  useEffect(() => {
    const root = document.documentElement;
    root.classList.remove('light');
    root.setAttribute('data-skin', skin);
    localStorage.setItem('theme', 'dark');
  }, [skin]);

  const toggleTheme = () => {
    // No-op: light mode is deprecated and removed
  };
  const setSkin = (s: Skin) => setSkinState(s);

  return (
    <ThemeContext.Provider value={{ theme: 'dark', skin, toggleTheme, setSkin }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
