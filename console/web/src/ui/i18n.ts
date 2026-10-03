import { useSyncExternalStore } from 'react';

export type Language = 'en' | 'tr';
export const languageStorageKey = 'ai-scientist.locale.v1';

function storedLanguage(): Language {
  try {
    return window.localStorage.getItem(languageStorageKey) === 'tr' ? 'tr' : 'en';
  } catch {
    return 'en';
  }
}

let language: Language = storedLanguage();
const listeners = new Set<() => void>();

export function t(english: string, turkish: string): string {
  return language === 'tr' ? turkish : english;
}

export function locale(): 'en-US' | 'tr-TR' {
  return language === 'tr' ? 'tr-TR' : 'en-US';
}

function updateDocument() {
  if (typeof document === 'undefined') return;
  document.documentElement.lang = language;
  document.title = t('AI Scientist · Control center', 'AI Scientist · Kontrol merkezi');
  document.querySelector('meta[name="description"]')?.setAttribute(
    'content', t('Local AI Scientist research control center', 'Yerel AI Scientist araştırma kontrol merkezi'),
  );
}

function notifyLanguage(next: Language) {
  language = next;
  updateDocument();
  listeners.forEach(listener => listener());
}

export function setLanguage(next: Language) {
  if (next !== 'en' && next !== 'tr') return;
  try { window.localStorage.setItem(languageStorageKey, next); } catch { /* Session selection still works. */ }
  notifyLanguage(next);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useI18n() {
  const current = useSyncExternalStore(subscribe, () => language, () => 'en' as Language);
  return { language: current, setLanguage, t, locale: locale() };
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === languageStorageKey || event.key === null) {
      notifyLanguage(storedLanguage());
    }
  });
  updateDocument();
}
