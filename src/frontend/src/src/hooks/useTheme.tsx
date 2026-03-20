import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export type Theme = 'dark' | 'light';
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
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem('theme');
    return (stored === 'light' || stored === 'dark') ? stored : 'dark';
  });
  const [skin, setSkinState] = useState<Skin>(() => {
    const stored = localStorage.getItem('skin');
    return (stored === 'retro' || stored === 'default') ? stored : 'default';
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'light') {
      root.classList.add('light');
    } else {
      root.classList.remove('light');
    }
    root.setAttribute('data-skin', skin);
    localStorage.setItem('theme', theme);
  }, [theme]);

  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute('data-skin', skin);
    localStorage.setItem('skin', skin);
  }, [skin]);

  const toggleTheme = () => setTheme(prev => prev === 'dark' ? 'light' : 'dark');
  const setSkin = (s: Skin) => setSkinState(s);

  return (
    <ThemeContext.Provider value={{ theme, skin, toggleTheme, setSkin }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
