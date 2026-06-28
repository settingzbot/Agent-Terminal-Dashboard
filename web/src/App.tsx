import { useMemo } from 'react'
import { ClaudeTerminalCard } from './components/ClaudeTerminalCard'
import { useIsMobile } from './hooks/useIsMobile'
import { deriveTheme, hslToHex } from './theme'
import type { Theme } from './theme'

export default function App() {
  const isMobile = useIsMobile()

  const theme: Theme = useMemo(() => deriveTheme(0), []) // dark theme (brightness 0)
  const accent = useMemo(() => hslToHex(43, 95, 50), []) // amber accent

  return (
    <div style={{
      width: '100vw',
      height: '100dvh',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
      background: theme.bg0,
      color: theme.text,
      fontFamily: theme.fontBody,
    }}>
      <ClaudeTerminalCard
        theme={theme}
        accent={accent}
        isMobile={isMobile}
        isActive={true}
      />
    </div>
  )
}
