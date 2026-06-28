// Returns the iOS software keyboard's height in CSS px while it's open, or 0
// while it's closed. Callers stack their own CSS off this value.
//
// Mechanical model (researched 2026-05-06; supersedes the prior
// "useChatBottomOffset" hook which mistakenly tried to undo iOS's auto-shift
// via window.scrollTo(0, 0) -- a no-op, since iOS pans the *visual viewport*
// over an unmoved *layout viewport*, not the document):
//
//   - The layout viewport (the ICB that `position: fixed` anchors to) keeps
//     its full pre-keyboard height. `window.innerHeight`, `100dvh`, and
//     `document.scrollingElement.scrollTop` all stay constant.
//   - The visual viewport shrinks (`vv.height` falls by the keyboard height)
//     and pans upward (`vv.offsetTop` becomes positive) so the focused input
//     is visually above the keyboard.
//   - `position: fixed; bottom: 0` therefore sits at the layout viewport
//     bottom -- behind the keyboard, off-screen -- regardless of `vv.offsetTop`.
//     Pinning a fixed-bottom element above the keyboard requires us to set
//     its `bottom` to the keyboard height ourselves.
//
// The canonical formula:
//   keyboardHeight = max(0, innerHeight - vv.height - vv.offsetTop)
// is robust against both "keyboard up, not panned" (offsetTop=0) and
// "keyboard up, panned" (offsetTop>0) -- in both states it returns the keyboard's
// footprint on the layout viewport, which is what `bottom: Xpx` needs to clear.
//
// Subscribes to BOTH `resize` and `scroll` on `window.visualViewport`. iOS
// changes `height` via resize and `offsetTop` via scroll independently across
// the keyboard show/hide animation, so we need both for the math to track.
//
// See docs/claude/synth/2026-05-06_ios26-safari-keyboard-reference.md for the
// audit and citations (WICG visual-viewport explainer, bram.us, WebKit bugs
// 259770/297779, Apple devforum 800125).
//
// Standalone-mode addendum (2026-06-10): in home-screen web-app (PWA) mode,
// iOS 26 leaves the app frame displaced after the keyboard dismisses.
// Confirmed variant on Nathan's phone via on-device instrumentation (a
// temporary debug overlay, since removed): the LAYOUT VIEWPORT ITSELF is
// left shrunk -
// window.innerHeight stuck at 793 vs outerHeight/screen.height 852, every
// other signal clean (scrollY 0, vv.offsetTop 0, vv.height == innerHeight).
// Fixed-bottom elements are correctly pinned to the bottom of a frame whose
// floor sits 59px (the top safe-area inset) above the physical screen
// bottom. This shrunk-frame variant was an iOS 26.0 bug that Apple FIXED in
// 26.1. The only content cure that ever worked was an automated tab-flip
// (flip to another tab and back to force WebKit to rebuild the layout), but
// it fired spuriously on ordinary tab interactions -- a visible Settings flash
// when, e.g., opening a report -- so it was retired 2026-06-14 now that the OS
// bug is gone. The two milder stuck variants (leftover document scroll,
// vv.offsetTop residue) are still detected and handled with proportionate,
// invisible content-level cures. A single early post-blur check misses them
// because the dismiss animation runs ~250-300ms and the displacement often
// only settles after it ends; hence the retry tail below.

import { useEffect, useState } from 'react';

const KEYBOARD_PRESENCE_THRESHOLD_PX = 50;

// Post-blur re-check schedule for the stuck-viewport recovery. The keyboard
// dismiss animation is ~250-300ms; the tail starts after it and extends well
// past, because the stuck state only materializes once the animation
// settles -- and a HEALTHY frame restore can itself land as late as ~400ms,
// so checking earlier produces false positives (the tab-flip cure fired its
// black blink on frames that were about to restore on their own -
// observed 2026-06-10).
const RECOVERY_DELAYS_MS = [250, 600, 1000, 1600];

// innerHeight shrinkage beyond this (vs. its pre-keyboard value) is treated
// as "layout viewport not restored". Safari-tab URL-bar jitter stays below it.
const STUCK_INNER_HEIGHT_TOLERANCE_PX = 24;

export function useKeyboardHeightPx(): number {
  const [kbd, setKbd] = useState(0);

  useEffect(() => {
    const vv = window.visualViewport;
    // No visualViewport (older browsers, server render) -- keyboard never shows.
    if (!vv) return;

    const update = () => {
      const next = Math.max(
        0,
        window.innerHeight - vv.height - vv.offsetTop,
      );
      setKbd(next > KEYBOARD_PRESENCE_THRESHOLD_PX ? next : 0);
    };

    // iOS 26 has a regression where the viewport doesn't always revert
    // cleanly after blur -- `vv.offsetTop` stays > 0 (WebKit bug 297779;
    // chronic in home-screen web-app mode, residual on iPad in Safari-tab
    // mode). Recovery: after any blur anywhere in the document, re-check on
    // a tail of delays and, if the pan is still stuck once the keyboard
    // should be fully down, force WebKit to re-clamp by performing a real
    // 1px scroll. A genuine scroll event is what re-clamps the visual
    // viewport -- plain scrollTo to the same position fires nothing. The
    // nudge-out and restore are split across a frame so WebKit can't
    // coalesce them into a net-zero no-op.
    const editableFocused = (): boolean => {
      const el = document.activeElement;
      return el instanceof HTMLElement && (
        el.isContentEditable ||
        el.tagName === 'INPUT' ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'SELECT'
      );
    };

    // Pre-keyboard baselines, captured at focusin (which fires before the
    // keyboard starts animating, so the synchronous reads are the "good"
    // values). scrollY catches the confirmed standalone variant (iOS scrolls
    // the document to reveal the input and never scrolls back); innerHeight
    // catches the shrunk-layout-viewport variant -- in both, every live value
    // looks internally consistent, so only a before/after comparison detects
    // the stuck state.
    let baselineInnerHeight = window.innerHeight;
    let baselineScrollY = window.scrollY;
    let recoveryTimers: number[] = [];
    const cancelRecovery = () => {
      recoveryTimers.forEach(t => window.clearTimeout(t));
      recoveryTimers = [];
    };

    // attempts/lastAction are debugging breadcrumbs left over from the
    // 2026-06-10 on-device investigation; they cost nothing and remain
    // readable from a remote console as window.__kbdDebug.
    const dbg = ((window as unknown as {
      __kbdDebug?: { attempts: number; lastAction: string };
    }).__kbdDebug ??= { attempts: 0, lastAction: '-' });

    // The shrunk-frame variant -- window.innerHeight left ~59px short (the top
    // safe-area inset) after a keyboard dismiss -- was an iOS 26.0 WebKit bug
    // that Apple fixed in 26.1 (apache/cordova-ios#1575 tracks the identical
    // WKWebView symptom). It used to be healed by an automated tab-flip
    // (dispatch FRAME_REBUILD_EVENT -> DashboardApp flips to Settings and
    // back), but that detour fired spuriously on non-keyboard tab interactions
    // (visible as a Settings flash when opening a report) and the OS bug it
    // cured is gone, so the tab-flip cure was retired 2026-06-14. The two
    // milder, content-level cures below (leftover document scroll, vv.offsetTop
    // residue) stay -- they're real and harmless. A shrunk frame is now only
    // detected (for the touchmove guard / debug breadcrumbs), not cured.
    const attemptRecovery = () => {
      // Keyboard legitimately up again (focus moved to another input) --
      // the displacement is intentional; stand down until the next blur.
      if (editableFocused()) {
        cancelRecovery();
        return;
      }
      // Keyboard still mid-dismiss -- innerHeight readings taken now look
      // exactly like the stuck frame and would fire the cure spuriously.
      // Skip; a later stage in the tail re-checks after the animation ends.
      const kbdFootprint = Math.max(
        0,
        window.innerHeight - vv.height - vv.offsetTop,
      );
      if (kbdFootprint > KEYBOARD_PRESENCE_THRESHOLD_PX) return;
      const scrollStuck = window.scrollY > baselineScrollY + 1;
      const panStuck = vv.offsetTop > 0;
      const heightStuck =
        window.innerHeight < baselineInnerHeight - STUCK_INNER_HEIGHT_TOLERANCE_PX;
      dbg.attempts += 1;
      dbg.lastAction = scrollStuck ? `scroll>${baselineScrollY}`
        : panStuck ? 'pan-nudge'
        : heightStuck ? 'height-stuck (uncured -- iOS 26.0 only)'
        : 'clean';
      if (scrollStuck) {
        // The fix tab-switching performs (handleSelectTab's scrollTo(0,0)),
        // minus the tab switch: putting the document back where it was before
        // the keyboard both undoes the leftover scroll and forces WebKit to
        // repaint the displaced fixed-bottom layers at their true positions.
        window.scrollTo({ top: baselineScrollY, behavior: 'auto' });
      } else if (panStuck) {
        const docEl = document.documentElement;
        const scroller = document.scrollingElement ?? docEl;
        // If the page exactly fits the viewport there is no scroll range and
        // scrollBy is a silent no-op -- grow the document 2px for one frame so
        // the nudge produces a real scroll event WebKit will re-clamp on.
        const hasRange = scroller.scrollHeight > scroller.clientHeight;
        if (!hasRange) docEl.style.minHeight = `${docEl.clientHeight + 2}px`;
        window.scrollBy(0, baselineScrollY > 0 ? -1 : 1);
        requestAnimationFrame(() => {
          window.scrollTo(0, baselineScrollY);
          if (!hasRange) docEl.style.minHeight = '';
        });
      }
      update();
    };

    const onFocusIn = () => {
      cancelRecovery();
      // Only adopt new baselines when the current frame is healthy -- if the
      // user refocuses an input while the viewport is still stuck shrunk,
      // keeping the old (healthy) baselines lets the next blur's recovery
      // still detect the shrink instead of accepting 793 as normal.
      if (window.innerHeight >= baselineInnerHeight) {
        baselineInnerHeight = window.innerHeight;
        baselineScrollY = window.scrollY;
      }
    };
    const onFocusOut = () => {
      cancelRecovery();
      RECOVERY_DELAYS_MS.forEach((ms) => {
        recoveryTimers.push(window.setTimeout(() => attemptRecovery(), ms));
      });
    };
    // A finger drag after blur means the user is scrolling on purpose -- the
    // gesture itself re-clamps WebKit's viewport, and yanking scrollY back
    // mid-gesture would fight them. Taps (touchstart without move) don't
    // cancel; only real movement does. EXCEPTION: when the frame is
    // currently shrunk, no gesture can heal it (confirmed on-device) and
    // cancelling would strand the bar mid-screen until the next blur -- let
    // the tail keep running toward the tab-flip cure.
    const onTouchMove = () => {
      const frameShrunk =
        window.innerHeight < baselineInnerHeight - STUCK_INNER_HEIGHT_TOLERANCE_PX;
      if (!frameShrunk) cancelRecovery();
    };

    // Rotation legitimately changes innerHeight; without re-baselining, a
    // portrait baseline (852) would make every blur in landscape (393) look
    // like a stuck frame and fire the cures forever. Re-read after the
    // rotation settles.
    let orientationTimer = 0;
    const onOrientationChange = () => {
      cancelRecovery();
      window.clearTimeout(orientationTimer);
      orientationTimer = window.setTimeout(() => {
        baselineInnerHeight = window.innerHeight;
        baselineScrollY = window.scrollY;
        update();
      }, 400);
    };

    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    document.addEventListener('focusin', onFocusIn, true);
    document.addEventListener('focusout', onFocusOut, true);
    document.addEventListener('touchmove', onTouchMove, true);
    window.addEventListener('orientationchange', onOrientationChange);
    return () => {
      cancelRecovery();
      window.clearTimeout(orientationTimer);
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
      document.removeEventListener('focusin', onFocusIn, true);
      document.removeEventListener('focusout', onFocusOut, true);
      document.removeEventListener('touchmove', onTouchMove, true);
      window.removeEventListener('orientationchange', onOrientationChange);
    };
  }, []);

  return kbd;
}
