/** `GET /api/atrium_ddns/events` — the log search.
 *
 * Same rule as `board.ts`, applied to a different surface: **the shapes
 * are restated here, the decisions are not.** The filter vocabulary,
 * the null-backend sentinel, the retention window, whether this caller
 * reads across tenants and whether an empty page means *filtered out*
 * or *never logged* all arrive computed. There is no `if` in this
 * bundle that re-derives any of them.
 *
 * That is not tidiness. The alternative — a frontend holding its own
 * list of event types, its own spelling of the null sentinel, its own
 * `permissions.includes('atrium_ddns.events.read.all')` — is four
 * second implementations of rules the server already applies, each of
 * which drifts silently. A filter option the server cannot produce
 * renders as a dropdown entry that matches nothing, and to a user that
 * is indistinguishable from "this has not happened lately".
 *
 * ## Why the filters are query parameters and not a POST body
 *
 * They are the URL, and the URL is the feature. Every filter
 * combination is a link somebody can send, bookmark, or reach
 * pre-applied from a device row on the board. A POST body cannot be any
 * of those things.
 */
import { queryOptions } from '@tanstack/react-query';

import { apiGet } from './http';

/** One line of the log.
 *
 * **The names are the display and the ids are the controls**, and both
 * halves of every pair are carried because they answer different
 * questions. `device_name` is captured at write time and survives the
 * device being deleted — it is what the row *says*. `device_id` is
 * `ON DELETE SET NULL`, so it is `null` exactly when the device is
 * gone — it is what the row can be *filtered by*.
 *
 * A renderer given only the id shows a wall of blanks for a deleted
 * device, which is the state the log is most often opened to
 * investigate. One given only the name cannot offer "show me
 * everything this device did".
 */
export interface EventRow {
  id: number;
  created_at: string;

  user_id: number | null;
  user_email: string | null;
  device_id: number | null;
  device_name: string | null;
  domain_id: number | null;
  domain_name: string | null;
  hostname_id: number | null;
  hostname: string | null;

  event_type: string;
  response_code: string | null;
  client_ip: string | null;
  /** The address the request was *about* (`myip`, normalised). Not the
   *  same fact as `client_ip`, and the difference is the interesting
   *  part of a NAT'd update. */
  ip: string | null;
  backend_type: string | null;
  message: string | null;
}

/** The filter values this installation can actually produce.
 *
 * Derived on the server from the writer's own constants and the
 * provider registry, so a provider deleted upstream takes its filter
 * option with it. The frontend renders this list and cannot add to it. */
export interface EventVocabulary {
  event_types: string[];
  response_codes: string[];
  backend_types: string[];
  /** The sentinel meaning `backend_type IS NULL`. Transported rather
   *  than duplicated — two spellings of a sentinel is a filter that
   *  silently stops matching. */
  backend_type_none: string;
  /** `worker_jobs.SUCCESS_RESPONSE_CODES`, verbatim.
   *
   * The log's one accented rendering keys off this. `ui-design.md`
   * §1.2 Rule 2 — `--ddns-diverge` appears nowhere except on a measured
   * disagreement — and on this surface a non-success response code *is*
   * the measured disagreement. A hardcoded `['good', 'nochg']` here
   * would be a second implementation of a classification the
   * health-check job owns, and the two would part company the first
   * time a code was reclassified, silently, because both renderings
   * look correct. */
  success_response_codes: string[];
}

/** The filters the query actually ran with, echoed back. */
export interface EventFilters {
  user_id: number | null;
  device_id: number | null;
  domain_id: number | null;
  hostname_id: number | null;
  event_type: string | null;
  response_code: string | null;
  backend_type: string | null;
  client_ip: string | null;
  since: string | null;
  until: string | null;
}

export interface EventPage {
  rows: EventRow[];
  next_cursor: string | null;
  limit: number;
  filters: EventFilters;
  vocabulary: EventVocabulary;
  retention_days: number;
  cross_tenant: boolean;
  /** **Three states.** `null` — not measured, because the page had
   *  rows. `true` — the scope holds rows but none match these filters.
   *  `false` — the scope holds no rows at all.
   *
   *  Three different empty panels with three different next actions.
   *  Collapsed into a boolean, a working filter looks like an empty
   *  account. */
  any_rows_in_scope: boolean | null;
  /** Filters that ran and structurally cannot have matched **for this
   *  caller**, each with its reason.
   *
   * Two ways it happens and they are different sentences. A typo'd
   * provider returns zero rows and reads like "no traffic for that
   * provider". A tenant-scoped `response_code=badauth` returns zero
   * rows and reads like "my credentials are fine" — those lines are
   * written before any device is identified, so they belong to no
   * account. Both are false negatives carrying the authority of a
   * measurement. */
  unmatchable_filters: UnmatchableFilter[];
}

export interface UnmatchableFilter {
  /** `key=value`, the reader's own input. */
  filter: string;
  /** One sentence, in the interface's voice. */
  reason: string;
}

/** The filter state, as the UI holds it. Every field is a string
 *  because that is what a query string holds and what a `<select>`
 *  returns; the empty string means *not filtered*, which is why it is
 *  distinguishable from `'0'`. */
export interface LogQuery {
  user_id: string;
  device_id: string;
  domain_id: string;
  hostname_id: string;
  event_type: string;
  response_code: string;
  backend_type: string;
  client_ip: string;
  since: string;
  until: string;
}

export const EMPTY_QUERY: LogQuery = {
  user_id: '',
  device_id: '',
  domain_id: '',
  hostname_id: '',
  event_type: '',
  response_code: '',
  backend_type: '',
  client_ip: '',
  since: '',
  until: '',
};

/** The keys of `LogQuery`, derived from the value rather than listed.
 *
 * Everything that walks the filter set — the URL reader, the URL
 * writer, the "how many are active" count, the chip strip — walks this.
 * A filter added to `EMPTY_QUERY` is therefore reachable from all four
 * without any of them being edited, which is the difference between one
 * source of truth and five that agree today. */
export const QUERY_KEYS = Object.keys(EMPTY_QUERY) as (keyof LogQuery)[];

/** How many filters are set. The denominator for "showing everything"
 *  against "showing a slice", and the thing the clear-filters control
 *  is enabled by. */
export function activeFilterCount(query: LogQuery): number {
  return QUERY_KEYS.filter((key) => query[key] !== '').length;
}

/** `LogQuery` -> a query string, dropping the unset ones.
 *
 * Unset filters are **absent**, never `?device_id=`. An empty-string
 * parameter is a value the server would have to decide the meaning of,
 * and "empty means unset" is exactly the collapse that makes
 * `backend_type` need a sentinel in the first place. */
export function toSearchParams(
  query: LogQuery,
  extra: Record<string, string> = {},
): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of QUERY_KEYS) {
    if (query[key] !== '') params.set(key, query[key]);
  }
  for (const [key, value] of Object.entries(extra)) {
    if (value !== '') params.set(key, value);
  }
  return params;
}

/** The inverse. Unknown parameters are dropped rather than carried:
 *  a `?nonsense=1` in a pasted link must not become a filter the server
 *  has never heard of. */
export function fromSearchParams(search: string): LogQuery {
  const params = new URLSearchParams(search);
  const out = { ...EMPTY_QUERY };
  for (const key of QUERY_KEYS) {
    out[key] = params.get(key) ?? '';
  }
  return out;
}

export const EVENTS_QUERY_KEY = ['atrium_ddns', 'events'] as const;

export async function getEvents(
  query: LogQuery,
  cursor: string | null,
): Promise<EventPage> {
  const params = toSearchParams(query, cursor ? { cursor } : {});
  const suffix = params.toString();
  return apiGet<EventPage>(
    `/atrium_ddns/events${suffix ? `?${suffix}` : ''}`,
  );
}

/** Shared query options.
 *
 * `refetchInterval` is deliberately absent, for the same reason the
 * board has none: a log that repolls faster than rows arrive produces
 * motion `docs/ops/ui-design.md` §3.7 spent a section removing, and a
 * search result that reorders under the reader's cursor is worse than a
 * stale one. There is a refresh control instead.
 *
 * Unlike the board there is **no `enabled` gate**, and that is the
 * frontend half of the two-reaches rule: any authenticated caller may
 * read their own log, so there is no permission to check before
 * fetching. The one permission-shaped decision on this surface —
 * whether the user column means anything — arrives as `cross_tenant` in
 * the response rather than being re-derived from the caller's
 * permission list. */
export function eventsQuery(query: LogQuery, cursor: string | null) {
  return queryOptions({
    queryKey: [...EVENTS_QUERY_KEY, query, cursor] as const,
    queryFn: () => getEvents(query, cursor),
  });
}
