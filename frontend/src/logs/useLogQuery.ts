/** The filter state lives in the URL, and that is the feature.
 *
 * The issue asks for filters "reachable pre-applied from any device or
 * hostname row". Held in React state they are reachable from nowhere:
 * not from the board, not from a bookmark, not from a link pasted into
 * a ticket. Held in the query string they are reachable from all three,
 * and the back button works for free.
 *
 * ## Why this is `window.location` and `popstate` rather than a router
 *
 * The host bundle mounts its own React tree inside atrium's
 * (`makeWrapperElement`), so react-router's context does not cross the
 * boundary and `useSearchParams` is not available here — importing
 * react-router into the bundle would give this tree a *second* router
 * with its own idea of the location, which is worse than not having
 * one.
 *
 * So: read `window.location.search`, write with `history.replaceState`,
 * and listen to `popstate` for the back button.
 *
 * **`replaceState`, not `pushState`, and the reason is atrium's router
 * rather than taste.** Atrium's SPA owns the history stack; a filter
 * change that pushed an entry would make the back button walk filter
 * states that atrium's own router knows nothing about, and its
 * `useLocation` would be one entry behind the address bar. Replacing
 * keeps the address bar shareable — which is the whole point — without
 * inventing history atrium cannot see. Navigation between *pages* stays
 * atrium's job.
 *
 * The path is preserved exactly: only `search` is rewritten, so a
 * filter change cannot move the user off the route.
 */
import { useCallback, useEffect, useState } from 'react';

import {
  EMPTY_QUERY,
  fromSearchParams,
  toSearchParams,
  type LogQuery,
} from '../api/events';

/** Read the current query string. Guarded for a non-browser host —
 *  vitest's jsdom provides `window`, but a server render would not, and
 *  a throw here would take the whole bundle down at import time. */
export function readLocationQuery(): LogQuery {
  if (typeof window === 'undefined') return { ...EMPTY_QUERY };
  return fromSearchParams(window.location.search);
}

export interface UseLogQuery {
  query: LogQuery;
  /** Replace the whole filter set — used by "clear all" and by a
   *  pre-applied link arriving through `popstate`. */
  setQuery: (next: LogQuery) => void;
  /** Set one filter. `''` clears it. */
  setFilter: (key: keyof LogQuery, value: string) => void;
  clear: () => void;
}

export function useLogQuery(): UseLogQuery {
  const [query, setQueryState] = useState<LogQuery>(readLocationQuery);

  // The back button, and any other agent that changes the location.
  // Without this the address bar and the rendered filters part company
  // and the *address bar* is the one that is right, which is the worst
  // way round: the user is looking at it.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onPop = () => setQueryState(readLocationQuery());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const write = useCallback((next: LogQuery) => {
    setQueryState(next);
    if (typeof window === 'undefined') return;
    const params = toSearchParams(next).toString();
    const url = `${window.location.pathname}${params ? `?${params}` : ''}`;
    window.history.replaceState(window.history.state, '', url);
  }, []);

  const setFilter = useCallback(
    (key: keyof LogQuery, value: string) => {
      write({ ...readLocationQuery(), ...{ [key]: value } } as LogQuery);
    },
    [write],
  );

  const clear = useCallback(() => write({ ...EMPTY_QUERY }), [write]);

  return { query, setQuery: write, setFilter, clear };
}
