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
