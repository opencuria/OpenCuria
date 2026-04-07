import { ref, watchEffect, type Ref } from 'vue'
import { usePreferredColorScheme } from '@vueuse/core'

export type ThemeMode = 'light' | 'dark' | 'auto'

const mode = ref<ThemeMode>('auto')

/**
 * Composable for managing the application theme.
 *
 * Supports three modes: 'light', 'dark', and 'auto' (system default).
 * Persists the user's choice in localStorage.
 */
export function useTheme(): {
  mode: Ref<ThemeMode>
  isDark: Ref<boolean>
  setTheme: (m: ThemeMode) => void
  toggleTheme: () => void
} {
  const systemPreference = usePreferredColorScheme()
  const isDark = ref(false)

  // Load from localStorage on first call
  const stored = localStorage.getItem('opencuria-theme') as ThemeMode | null
  if (stored && ['light', 'dark', 'auto'].includes(stored)) {
    mode.value = stored
  }

  watchEffect(() => {
    let resolved: 'light' | 'dark'
    if (mode.value === 'auto') {
      resolved = systemPreference.value === 'dark' ? 'dark' : 'light'
    } else {
      resolved = mode.value
    }

    isDark.value = resolved === 'dark'

    // Toggle class on <html> for Tailwind dark mode (class strategy)
    // and data attribute for CSS variable theming
    const root = document.documentElement
    if (resolved === 'dark') {
      root.classList.add('dark')
      root.setAttribute('data-theme', 'dark')
    } else {
      root.classList.remove('dark')
      root.setAttribute('data-theme', 'light')
    }
  })

  function setTheme(m: ThemeMode): void {
    mode.value = m
    localStorage.setItem('opencuria-theme', m)
  }

  function toggleTheme(): void {
    const next: ThemeMode = mode.value === 'light' ? 'dark' : mode.value === 'dark' ? 'auto' : 'light'
    setTheme(next)
  }

  return { mode, isDark, setTheme, toggleTheme }
}
