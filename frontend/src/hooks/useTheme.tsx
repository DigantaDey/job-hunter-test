import { createContext, useContext, useEffect, useState } from 'react'
type Theme = 'light' | 'dark' | 'system'
const Ctx = createContext<{theme: Theme, setTheme: (t: Theme)=>void, resolved: 'light'|'dark'}>(null as any)
export function ThemeProvider({children}: {children: React.ReactNode}) {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem('theme') as Theme) || 'system')
  const [resolved, setResolved] = useState<'light'|'dark'>('light')
  useEffect(()=>{
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const compute = () => {
      const r = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme
      setResolved(r as any)
      document.documentElement.classList.toggle('dark', r==='dark')
    }
    compute()
    localStorage.setItem('theme', theme)
    media.addEventListener('change', compute)
    return ()=> media.removeEventListener('change', compute)
  }, [theme])
  return <Ctx.Provider value={{theme, setTheme, resolved}}>{children}</Ctx.Provider>
}
export const useTheme = () => useContext(Ctx)
