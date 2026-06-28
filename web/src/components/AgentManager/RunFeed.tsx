// Structured activity feed for one agent run (issue #32). Renders the ordered
// events assembled server-side from the run's Claude Code transcript
// (GET /api/agents/runs/{id}/feed) -- the human prompt, assistant text, each
// tool call and its result -- plus a token/cost summary. While the run is live
// the feed is polled; a finished run is fetched once.
//
// Reading happens off the transcript on disk, so a just-launched run with no
// transcript yet shows an empty-but-valid feed (not an error).

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { getRunFeed, type FeedEvent, type FeedUsage, type RunFeed as RunFeedData } from '../../api/agents';
import { type Theme } from '../../theme';

type Props = {
  runId: string;
  theme: Theme;
  accent: string;
  // True while the run is queued/running -- poll for new events. A finished run
  // is loaded once and left alone.
  live: boolean;
};

// Imperative handle so the parent's "Latest" button can snap this feed to the
// newest event (and re-arm auto-follow) without the parent reaching into our
// scroll container directly.
export type RunFeedHandle = { scrollToBottom: () => void };

const POLL_MS = 2000;

export const RunFeed = forwardRef<RunFeedHandle, Props>(function RunFeed({ runId, theme, accent, live }, ref) {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [usage, setUsage] = useState<FeedUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoFollow = useRef(true);
  // Track the last event count so we can auto-scroll on new events
  const prevCount = useRef(0);

  const fetchFeed = useCallback(async (signal?: AbortSignal) => {
    try {
      const feed: RunFeedData = await getRunFeed(runId, signal);
      if (signal?.aborted) return;
      setEvents(feed.events);
      setUsage(feed.usage);
      setError(null);
    } catch (e) {
      if (!signal?.aborted) {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [runId]);

  // Poll while live; fetch once for finished runs.
  useEffect(() => {
    const ctrl = new AbortController();
    if (live) {
      void fetchFeed(ctrl.signal);
      const t = setInterval(() => { void fetchFeed(ctrl.signal); }, POLL_MS);
      return () => { ctrl.abort(); clearInterval(t); };
    } else {
      void fetchFeed(ctrl.signal);
      return () => ctrl.abort();
    }
  }, [fetchFeed, live]);

  // Auto-scroll to bottom when new events arrive and auto-follow is on.
  useEffect(() => {
    if (events.length > prevCount.current && autoFollow.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
    prevCount.current = events.length;
  }, [events]);

  // Expose scrollToBottom for the parent's "Latest" button.
  useImperativeHandle(ref, () => ({
    scrollToBottom() {
      autoFollow.current = true;
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
    },
  }), []);

  const scrollHandler = () => {
    const el = scrollRef.current;
    if (!el) return;
    // If the user scrolls up, disable auto-follow; if they're at the bottom, re-enable.
    autoFollow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const toggleExpand = (id: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (loading) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
      }}>loading feed…</div>
    );
  }

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <div ref={scrollRef} onScroll={scrollHandler}
        style={{
          flex: 1, minHeight: 0, overflowY: 'auto', padding: '8px 12px',
          display: 'flex', flexDirection: 'column', gap: 6,
        }}>
        {error && (
          <div style={{
            padding: '8px 12px', fontFamily: theme.fontMono, fontSize: 10,
            color: theme.red, background: `${theme.red}14`, borderRadius: 4,
          }}>{error}</div>
        )}
        {events.length === 0 && !error && (
          <div style={{
            flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
          }}>no events yet</div>
        )}
        {events.map((ev, i) => (
          <EventCard key={i} event={ev} theme={theme} accent={accent}
            expanded={expanded.has(`${i}`)} onToggle={() => toggleExpand(`${i}`)} />
        ))}
      </div>
      {usage && (
        <UsageBar usage={usage} theme={theme} />
      )}
    </div>
  );
});

function EventCard({ event: ev, theme, accent, expanded, onToggle }: {
  event: FeedEvent; theme: Theme; accent: string;
  expanded: boolean; onToggle: () => void;
}) {
  switch (ev.kind) {
    case 'prompt':
      return (
        <div style={{
          padding: '6px 10px', background: `${accent}0a`,
          borderLeft: `2px solid ${accent}`, borderRadius: 4,
          fontFamily: theme.fontBody, fontSize: 12, color: theme.text,
        }}>
          <div style={{ fontFamily: theme.fontMono, fontSize: 9, color: accent, marginBottom: 3, fontWeight: 700 }}>
            PROMPT {ev.ts ? <span style={{ color: theme.text3, fontWeight: 400 }}>{ev.ts}</span> : null}
          </div>
          <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.4 }}>{ev.text}</div>
        </div>
      );
    case 'text':
      return (
        <div style={{
          padding: '6px 10px', background: `${theme.bg2}44`, borderRadius: 4,
          fontFamily: theme.fontBody, fontSize: 12, color: theme.text, lineHeight: 1.4,
          whiteSpace: 'pre-wrap',
        }}>
          {ev.text}
        </div>
      );
    case 'tool_use':
      return (
        <div style={{
          padding: '6px 10px', background: `${theme.blue}0a`,
          borderLeft: `2px solid ${theme.blue}`, borderRadius: 4,
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text,
        }}>
          <div style={{ color: theme.blue, fontWeight: 600, marginBottom: 2 }}>
            tool: {ev.name ?? 'unknown'}
          </div>
          {ev.input && (
            <pre style={{
              fontSize: 10, color: theme.text2, margin: 0,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'pre-wrap',
            }}>
              {JSON.stringify(ev.input, null, 2).slice(0, 500)}
            </pre>
          )}
        </div>
      );
    case 'tool_result':
      return (
        <div style={{
          padding: '6px 10px', background: ev.is_error ? `${theme.red}0a` : `${theme.green}08`,
          borderLeft: `2px solid ${ev.is_error ? theme.red : theme.green}`, borderRadius: 4,
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text,
        }}>
          <div style={{ color: ev.is_error ? theme.red : theme.green, fontWeight: 600, marginBottom: 2 }}>
            {ev.is_error ? 'ERROR' : 'result'}
          </div>
          {ev.text && (
            <pre style={{
              fontSize: 10, color: theme.text2, margin: 0,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'pre-wrap',
            }}>
              {ev.text.slice(0, 1000)}{ev.text.length > 1000 ? '…' : ''}
            </pre>
          )}
        </div>
      );
    case 'stage':
      return (
        <div style={{
          padding: '6px 10px', background: `${theme.bg2}44`,
          borderLeft: `2px solid ${
            ev.status === 'ok' ? theme.green :
            ev.status === 'fail' ? theme.red :
            ev.status === 'skip' ? theme.text3 :
            theme.blue
          }`, borderRadius: 4,
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text, cursor: 'pointer',
        }} onClick={onToggle}>
          <div style={{ fontWeight: 600, marginBottom: 2 }}>
            <span style={{
              color: ev.status === 'ok' ? theme.green :
                     ev.status === 'fail' ? theme.red :
                     ev.status === 'skip' ? theme.text3 : theme.blue,
            }}>{ev.status.toUpperCase()}</span>
            {' '}{ev.stage}
            {ev.ts ? <span style={{ color: theme.text3, fontWeight: 400 }}> {String(ev.ts)}</span> : null}
          </div>
          <div style={{ color: theme.text2 }}>{ev.detail}</div>
          {ev.log && expanded && (
            <pre style={{
              marginTop: 6, padding: 6, background: theme.bg1, borderRadius: 4,
              fontSize: 10, color: theme.text2, overflow: 'auto', maxHeight: 200,
              whiteSpace: 'pre-wrap',
            }}>{ev.log}</pre>
          )}
        </div>
      );
    default:
      return null;
  }
}

function UsageBar({ usage, theme }: { usage: FeedUsage; theme: Theme }) {
  return (
    <div style={{
      display: 'flex', gap: 12, padding: '6px 12px',
      borderTop: `1px solid ${theme.border}`,
      fontFamily: theme.fontMono, fontSize: 9, color: theme.text2,
    }}>
      <span>{usage.model ?? '—'}</span>
      <span>in: {fmtTokens(usage.input_tokens)}</span>
      <span>out: {fmtTokens(usage.output_tokens)}</span>
      <span>cache: {fmtTokens(usage.cache_creation_input_tokens)} + {fmtTokens(usage.cache_read_input_tokens)}</span>
      <span>msgs: {usage.message_count}</span>
      <span style={{ color: theme.amber }}>~${usage.cost_usd.toFixed(2)}</span>
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}
