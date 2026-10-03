import { useSyncExternalStore } from 'react';

export type Theme = 'light' | 'dark';
export const themeStorageKey = 'ai-scientist.theme.v1';

function storedTheme(): Theme {
  try {
    return window.localStorage.getItem(themeStorageKey) === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

let theme: Theme = storedTheme();
const listeners = new Set<() => void>();

function updateDocument() {
  if (typeof document !== 'undefined') document.documentElement.dataset.theme = theme;
}

function notifyTheme(next: Theme) {
  theme = next;
  updateDocument();
  listeners.forEach(listener => listener());
}

export function setTheme(next: Theme) {
  if (next !== 'light' && next !== 'dark') return;
  try { window.localStorage.setItem(themeStorageKey, next); } catch { /* Session selection still works. */ }
  notifyTheme(next);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useTheme() {
  const current = useSyncExternalStore(subscribe, () => theme, () => 'light' as Theme);
  return { theme: current, setTheme };
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === themeStorageKey || event.key === null) notifyTheme(storedTheme());
  });
  updateDocument();
}
