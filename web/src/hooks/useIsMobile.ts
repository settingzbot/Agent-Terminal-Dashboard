import { useEffect, useState } from 'react';

// Single source of truth for the desktop/mobile breakpoint.
// 820px matches the pre-existing narrow-notice threshold and the bot's vanilla
// dashboard ([dashboard_frontend#sidebar---and-the-quick-stats-row] in the bot repo).
export const MOBILE_BREAKPOINT_PX = 820;

/**
 * Reactive boolean: true when the viewport is at or below
 * MOBILE_BREAKPOINT_PX.
 *
 * Deliberately width-only: a phone rotated to landscape (844-932 CSS px on
 * modern iPhones) gets the DESKTOP layout, and that's a feature -- Nathan
 * uses it as an escape hatch and likes having it on the phone (2026-06-10).
 * A `(pointer: coarse) and (max-height: 500px)` clause forcing mobile-in-
 * landscape was added and reverted the same day. Touch-input affordances on
 * the desktop layout must therefore key off pointer coarseness, NOT this
 * hook (see IS_COARSE_POINTER in ClaudeTerminalCard).
 */
export function useIsMobile(breakpointPx = MOBILE_BREAKPOINT_PX): boolean {
  const query = `(max-width: ${breakpointPx}px)`;
  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia(query).matches : false,
  );
  useEffect(() => {
    const mql = window.matchMedia(query);
    const update = (e: MediaQueryListEvent | MediaQueryList) => setIsMobile(e.matches);
    update(mql);
    mql.addEventListener('change', update);
    return () => mql.removeEventListener('change', update);
  }, [query]);
  return isMobile;
}
