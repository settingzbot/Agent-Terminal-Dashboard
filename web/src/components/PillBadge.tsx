// The four-pill HITL notification badge (#65, parent PRD #58). Renders the
// backend-derived pill counts as a compact cluster of colored count chips:
//
//   green  -- completed work awaiting acknowledgement
//   yellow -- supervised sign-off pending (SUPERVISED mode only; usually 0)
//   purple -- trading-surface approval waiting at the #63 money wall
//   red N  -- problems (an item halt OR an architecture freeze)
//
// A pill with a zero count is hidden, so a quiet system shows nothing. Used on
// both the desktop Sidebar's Claude nav item and the mobile BottomTabBar's Claude
// icon, fed by the same useAgentBadge() counts. The full views (the approve
// screen, the completed screen) live in the Claude tab's Agent Manager.

import type { Theme } from '../theme';
import type { PillCounts } from '../api/agents';

// purple has no theme token (the palette ships green/red/amber/blue only); the
// money-wall pill needs a distinct hue, so it gets a local constant tuned to the
// warm-beige palette rather than inventing a new theme token. One value reads on
// both dark and light cards.
export const PILL_PURPLE = '#b89bd9';

export type PillKind = 'green' | 'yellow' | 'purple' | 'red';

export function pillColor(kind: PillKind, theme: Theme): string {
  switch (kind) {
    case 'green': return theme.green;
    case 'yellow': return theme.amber;
    case 'purple': return PILL_PURPLE;
    case 'red': return theme.red;
  }
}

const ORDER: PillKind[] = ['green', 'yellow', 'purple', 'red'];

const TITLES: Record<PillKind, string> = {
  green: 'completed work -- open the completed view to clear',
  yellow: 'awaiting supervised sign-off',
  purple: 'trading-surface approval needed (money wall)',
  red: 'problems -- a halt or an architecture freeze',
};

/** A single count chip. Hidden when count <= 0. */
export function Pill({ kind, count, theme }: { kind: PillKind; count: number; theme: Theme }) {
  if (!(count > 0)) return null;
  const c = pillColor(kind, theme);
  return (
    <span
      title={`${TITLES[kind]} (${count})`}
      style={{
        minWidth: 14, height: 14, padding: '0 4px',
        borderRadius: 7,
        background: c,
        color: theme.bg0,
        fontFamily: theme.fontMono, fontSize: 9, fontWeight: 700,
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        lineHeight: 1, fontVariantNumeric: 'tabular-nums',
        boxShadow: `0 0 0 2px ${theme.bg1}`,
      }}
    >{count}</span>
  );
}

/** The compact four-pill cluster. Renders nothing when every count is zero. */
export function PillBadge({ counts, theme }: { counts: PillCounts; theme: Theme }) {
  const any = counts.green + counts.yellow + counts.purple + counts.red > 0;
  if (!any) return null;
  return (
    <span style={{ display: 'inline-flex', gap: 2, alignItems: 'center' }}>
      {ORDER.map(k => <Pill key={k} kind={k} count={counts[k]} theme={theme} />)}
    </span>
  );
}
