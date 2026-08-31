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

/** The names surface, filtered to one zone.
 *
 * A query parameter and not a path segment: the names page is a list
 * with filters, and "the names in zone 7" is one of those filters rather
 * than a different page. This is a view of another thing — and the zone
 * modal itself is `?zone=` for a different reason (a second route
 * unmounts the host root under the portal; see `DomainsPage`).
 *
 * The zone card used to render this list inline. It does not any more —
 * that is a different interface, and a form is not where you browse.
 */
/** Filters the names page honours. Read by `HostnamesPage`, written by
 *  the zone list and the device card — one spelling, so a link cannot
 *  point at a parameter nobody parses.
 *
 *  That is not hypothetical: `namesHrefForZone` shipped before the page
 *  read anything, so every "N names" link on the zone list landed on an
 *  unfiltered page and looked like it had worked. */
export const NAME_ZONE_PARAM = 'zone';
export const NAME_ID_PARAM = 'name';

export function namesHrefForZone(zoneId: number): string {
  return `/atrium-ddns/names?${NAME_ZONE_PARAM}=${zoneId}`;
}

/** The names surface, focused on one name. Same query-parameter shape:
 *  the names list is a list with filters, and "the name with id 7" is
 *  one of those rather than a page of its own. */
export function namesHrefForName(hostnameId: number): string {
  return `/atrium-ddns/names?${NAME_ID_PARAM}=${hostnameId}`;
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
/** A modal whose open-ness is a query parameter on a list route.
 *
 * Not a path segment, and the reason is a bug rather than a preference.
 * `/atrium-ddns/zones/:id` was a separate registered route, so opening
 * and closing the modal swapped atrium's route element — which unmounts
 * the host's React root. The modal is portalled to `document.body`, so
 * on close the root went away and the portal was orphaned.
 *
 * A query parameter never changes the route. Same mount, same portal,
 * only `search` moves — which is also the pattern the host SDK's own
 * `useAtriumLocation` docs use for exactly this shape. Reload, Back and
 * paste all still work, because the address still carries the state.
 *
 * Written once and shared by zones and devices. The second surface is
 * where a copy would have started to drift — the first one to gain, say,
 * a `?tab=` would have taught only its own parser about it.
 */
export type ModalTarget = { open: false } | { open: true; id: number | null };

/** `new`. A literal the id parser rejects, so "create" and "row N"
 *  cannot be confused for one another. */
export const NEW_VALUE = 'new';

/** What `?<param>=` means, in three states.
 *
 * Absent — no modal. `new` — the create form. A number — that row.
 * Three states and not two, because *closed* and *creating* are
 * different and a single nullable id cannot hold both.
 */
export function targetFromSearch(search: string, param: string): ModalTarget {
  const raw = new URLSearchParams(search).get(param);
  if (raw === null) return { open: false };
  if (raw === NEW_VALUE) return { open: true, id: null };
  // Only all-digits is an id. A junk value opens nothing rather than
  // opening "row NaN".
  if (!/^\d+$/.test(raw)) return { open: false };
  return { open: true, id: Number(raw) };
}

export function hrefWithTarget(
  path: string,
  param: string,
  value: number | typeof NEW_VALUE,
): string {
  return `${path}?${param}=${value}`;
}

export const ZONE_PARAM = 'zone';
export const DEVICE_PARAM = 'device';

export function zoneFromSearch(search: string): ModalTarget {
  return targetFromSearch(search, ZONE_PARAM);
}

export function zoneHrefParam(id: number): string {
  return hrefWithTarget(DOMAINS_PATH, ZONE_PARAM, id);
}

export function zoneNewHref(): string {
  return hrefWithTarget(DOMAINS_PATH, ZONE_PARAM, NEW_VALUE);
}

export function deviceFromSearch(search: string): ModalTarget {
  return targetFromSearch(search, DEVICE_PARAM);
}

export function deviceHrefParam(id: number): string {
  return hrefWithTarget(DEVICES_PATH, DEVICE_PARAM, id);
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
 * `deviceIdFromPath` is now the only pathname parser here. #88 added a
 * second for zones, and the note that lived here argued against merging
 * them; that argument expired when the zone route did — the zone modal
 * reads `?zone=` through the generic `targetFromSearch`, which is where
 * a third surface should go too.
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
