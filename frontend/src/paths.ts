/** The paths this bundle registers, in one module with no imports.
 *
 * They used to live in `main.tsx`, which is the file that registers
 * them — the right home right up until a *page* needed to name another
 * page. #75's help entry lists every surface this bundle owns, and
 * importing the constants from `main.tsx` would be a cycle: `main`
 * imports `HelpPage` imports `main`.
 *
 * Two paths are deliberately **not** here. `NAMES_PATH` lives in
 * `HostnamesPage` and `LOG_PATH` in `LogSearchPage`, because the board
 * and the zones page link to them and those modules already export them
 * for that reason; moving them would be churn for symmetry's sake.
 * `main.tsx` re-exports all five, so it stays the one place a reader
 * looks for *"what paths does this bundle own"*.
 */

/** The board's path. Exported so a test asserts the registered route
 *  against the same string the nav item points at, rather than against
 *  a literal typed twice. */
export const BOARD_PATH = '/atrium-ddns/board';

/** #45's two tenant surfaces, same rule. */
export const DOMAINS_PATH = '/atrium-ddns/domains';
export const DEVICES_PATH = '/atrium-ddns/devices';

/** #88's zone detail route — design §10.2, and §12 for why it is a route.
 *
 * The pattern as react-router matches it. Atrium drops a registered
 * `path` straight into `<Route path=…>` (`App.tsx`), so `:id` is an
 * ordinary segment parameter there. It is **not** available to the host
 * subtree through `useParams`: `makeWrapperElement` mounts a second
 * React root, and react-router's context does not cross it. The id is
 * read off the pathname instead, by :func:`zoneIdFromPath`, which is
 * the only place that parse exists.
 */
export const ZONE_ROUTE_PATH = '/atrium-ddns/zones/:id';

/** The href for one zone. Used by the list page and by the e2e spec, so
 *  the string is built once rather than concatenated at each call site. */
export function zoneHref(id: number): string {
  return `/atrium-ddns/zones/${id}`;
}

/** The names surface, filtered to one zone.
 *
 * A query parameter and not a path segment: the names page is a list
 * with filters, and "the names in zone 7" is one of those filters rather
 * than a different page. `ZONE_ROUTE_PATH` earns a segment because a
 * zone is a thing you open; this is a view of another thing.
 *
 * The zone card used to render this list inline. It does not any more —
 * that is a different interface, and a form is not where you browse.
 */
export function namesHrefForZone(zoneId: number): string {
  return `/atrium-ddns/names?zone=${zoneId}`;
}

/** The zone list, which is also where a zone modal is drawn over. */
export const ZONES_LIST_PATH = DOMAINS_PATH;

/** The open zone, as a **query parameter on the list route**.
 *
 * Not a path segment, and the reason is a bug rather than a preference.
 * `/atrium-ddns/zones/:id` was a separate registered route, so opening
 * and closing the modal swapped atrium's route element — which unmounts
 * the host's React root. The modal is portalled to `document.body`, so
 * on close the root went away and **the portal was orphaned**: the zone
 * was deleted, the list refreshed underneath, and the dialog stayed on
 * screen with nothing behind it.
 *
 * A query parameter never changes the route. Same mount, same portal,
 * only `search` moves — which is also the pattern the host SDK's own
 * `useAtriumLocation` docs use for exactly this shape. Reload, Back and
 * paste all still work, because the address still carries the state.
 */
export const ZONE_PARAM = 'zone';

/** `?zone=new`. A literal the id parser rejects, so "create" and "zone
 *  number N" cannot be confused for one another. */
export const ZONE_NEW_VALUE = 'new';

export function zoneHrefParam(id: number): string {
  return `${DOMAINS_PATH}?${ZONE_PARAM}=${id}`;
}

export function zoneNewHref(): string {
  return `${DOMAINS_PATH}?${ZONE_PARAM}=${ZONE_NEW_VALUE}`;
}

/** What `?zone=` means, in three states.
 *
 * `undefined` — no modal. `null` — the create form. A number — that
 * zone. Three states and not two, because *closed* and *creating* are
 * different and a single nullable id cannot hold both.
 */
export function zoneFromSearch(
  search: string,
): { open: false } | { open: true; id: number | null } {
  const raw = new URLSearchParams(search).get(ZONE_PARAM);
  if (raw === null) return { open: false };
  if (raw === ZONE_NEW_VALUE) return { open: true, id: null };
  // Same rule as `zoneIdFromPath`: only all-digits is an id. A junk
  // value opens nothing rather than opening "zone NaN".
  if (!/^\d+$/.test(raw)) return { open: false };
  return { open: true, id: Number(raw) };
}

/** The id in `/atrium-ddns/zones/:id`, or `null` when the pathname is
 *  not that route.
 *
 * Returns `null` rather than `NaN` for a non-numeric segment, and the
 * distinction is the point: `NaN` would flow into a lookup that finds
 * nothing and render as *"no such zone"*, which is a claim about the
 * tenant's data. A pathname that does not carry an id is a fact about
 * the URL, and the page says so with different words.
 */
export function zoneIdFromPath(pathname: string): number | null {
  const match = /^\/atrium-ddns\/zones\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  // `Number('')` is 0 and `Number('1x')` is NaN; only an all-digits
  // segment is an id here, and a leading `+`, a decimal point or an
  // exponent are all things `Number` would accept and a row id is not.
  if (!/^\d+$/.test(match[1])) return null;
  return Number(match[1]);
}

/** #89's device detail — `ui-design.md` §11.2.
 *
 * A **route**, and §12 decides that on the width budget rather than on
 * preference: one resolution strip needs ≈592px (§3.1), atrium's shell
 * gives 1168px at a 1440px viewport (§3.6), a 360/800 master-detail
 * split leaves ~790px, and Mantine's `lg` drawer is 620px — *below the
 * one-strip minimum*, so the signature element would wrap inside its
 * own detail view. A route keeps the full 1168px, is linkable into a
 * ticket, and leaves the back button working.
 *
 * The `:id` segment is react-router's, because atrium's `App.tsx`
 * hands every registered `path` straight to `<Route path=…>`. The page
 * itself cannot call `useParams` — the host bundle mounts its own React
 * tree, so react-router's context does not cross the boundary (the same
 * reason `useLogQuery` reads `window.location`) — so it parses the id
 * out of the pathname with `deviceIdFromPath` below. One string, two
 * readings, and they are kept in one module so they cannot drift.
 *
 * #88 landed `zoneIdFromPath` above against the same base, and the two
 * parsers are deliberately **not** merged into one generic helper. They
 * answer the same shape of question and the reasoning behind each is
 * written where it applies; a shared `idFromPath(pattern, path)` would
 * be a third thing to read before either page's URL handling made
 * sense. If a third detail route appears, that is the point to
 * generalise — two is not a pattern.
 */
export const DEVICE_DETAIL_PATH = '/atrium-ddns/devices/:id';

/** The href for one device. The inverse of `deviceIdFromPath`, and the
 *  only place a detail URL is composed. */
export function deviceHref(id: number): string {
  return `${DEVICES_PATH}/${id}`;
}

/** The id in a detail URL, or `null` when the path is not one.
 *
 * Derived from `DEVICE_DETAIL_PATH` rather than from a second literal:
 * the pattern is split on `/`, the `:id` segment is located by name,
 * and the same index is read out of the real path. A route renamed to
 * `/atrium-ddns/routers/:id` therefore keeps working without this
 * function being edited, which is the difference between deriving and
 * restating.
 *
 * Returns `null` — not `0`, and not `NaN` — for anything that is not a
 * positive integer id. The three states the caller has to tell apart
 * are *no id in the URL*, *an id that names nothing* and *a device*;
 * rendering the first as `0` would send a request for device zero.
 */
export function deviceIdFromPath(pathname: string): number | null {
  const pattern = DEVICE_DETAIL_PATH.split('/');
  const actual = pathname.replace(/\/+$/, '').split('/');
  if (actual.length !== pattern.length) return null;
  let id: number | null = null;
  for (let i = 0; i < pattern.length; i += 1) {
    if (pattern[i].startsWith(':')) {
      if (!/^[0-9]+$/.test(actual[i])) return null;
      const parsed = Number(actual[i]);
      if (!Number.isSafeInteger(parsed) || parsed <= 0) return null;
      id = parsed;
    } else if (pattern[i] !== actual[i]) {
      return null;
    }
  }
  return id;
}
