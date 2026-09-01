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

/** The board, filtered to one zone.
 *
 * Takes the zone's **name**, not its id: the board payload carries
 * `domain_name` on each hostname and no `domain_id`, so the name is what
 * the filter can actually compare against — and it is what the dropdown
 * shows, so the address matches the control. A renamed zone breaks a
 * stale link, which for a view filter is the right trade against a
 * backend change to carry an id nothing else needs. */
export function namesHrefForZone(zoneName: string): string {
  return `/atrium-ddns?${NAME_ZONE_PARAM}=${encodeURIComponent(zoneName)}`;
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

/** Where to go back to when a create flow finishes.
 *
 * The board is the landing surface and the only nav entry, but creating a
 * device or a name happens on `/atrium-ddns/devices` and
 * `/atrium-ddns/names` — pages that no longer have one. So a create started
 * from the board used to end on a page you could not navigate away from,
 * which reads as being dumped somewhere rather than as having finished.
 *
 * The alternative was hosting both create modals on the board. That is the
 * better shape and is not this change: the device form shares state with the
 * secret modal that follows it, and pulling them apart while the surface is
 * being tested is more churn than the problem is worth. Worth revisiting.
 */
export const RETURN_PARAM = 'from';

/** Adds a return address to a create href. */
export function withReturn(href: string, from: string): string {
  const sep = href.includes('?') ? '&' : '?';
  return `${href}${sep}${RETURN_PARAM}=${encodeURIComponent(from)}`;
}

/** The return address, or `null`.
 *
 * **Refuses anything not inside this bundle.** A return address is a
 * redirect target read from the URL, so an unchecked one sends a user
 * wherever a pasted link says — including off-site. It must start with
 * `/atrium-ddns`, and `//evil.example` is rejected because a protocol-
 * relative URL is not a path however much it looks like one.
 */
export function returnFromSearch(search: string): string | null {
  const raw = new URLSearchParams(search).get(RETURN_PARAM);
  if (raw === null) return null;
  if (raw.startsWith('//')) return null;
  if (!raw.startsWith('/atrium-ddns')) return null;
  return raw;
}

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

/** Which device a *new* name starts attached to.
 *
 * A separate key from `DEVICE_PARAM` on purpose. On the board `?device=`
 * opens that device's card, so reusing it for the preset would ask one
 * address to mean two things — open a card, and pre-fill a field in a
 * different modal — and `?name=new&device=7` would open both. */
export const NAME_FOR_PARAM = 'for';

/** The board — the only tenant surface — with one modal open over it.
 *
 * These replace `namesHrefForName` / `deviceHrefParam`, which pointed at
 * `/atrium-ddns/names` and `/atrium-ddns/devices`. The board hosts both
 * modals now, so opening one is a query on the page you are already on
 * rather than a trip to a page with no way back. */
export const BOARD_PATH_HOME = '/atrium-ddns';

export function boardNameHref(hostnameId: number): string {
  return `${BOARD_PATH_HOME}?${NAME_ID_PARAM}=${hostnameId}`;
}

export function boardNameNewHref(deviceId?: number): string {
  const base = `${BOARD_PATH_HOME}?${NAME_ID_PARAM}=${NEW_VALUE}`;
  return deviceId === undefined
    ? base
    : `${base}&${NAME_FOR_PARAM}=${deviceId}`;
}

/** The board, *filtered* to one device — not the device's card open.
 *
 * A separate key from `DEVICE_PARAM` because on the board `?device=`
 * opens the card, and this is the opposite move: close the card and show
 * the rows it was describing. One key cannot mean both, and
 * `?device=7&device=7` is not a design.
 *
 * It exists because the card used to list the device's names itself,
 * which is the board's job — the same rows, drawn a second way, in a
 * modal you opened to change a rate limit. The card links to the real
 * list instead of carrying a copy of it. */
export const BOARD_ONLY_DEVICE_PARAM = 'onlyDevice';

export function boardForDeviceHref(deviceId: number): string {
  return `${BOARD_PATH_HOME}?${BOARD_ONLY_DEVICE_PARAM}=${deviceId}`;
}

export function boardDeviceHref(deviceId: number): string {
  return `${BOARD_PATH_HOME}?${DEVICE_PARAM}=${deviceId}`;
}

export function zoneFromSearch(search: string): ModalTarget {
  return targetFromSearch(search, ZONE_PARAM);
}

export function zoneHrefParam(id: number): string {
  return hrefWithTarget(DOMAINS_PATH, ZONE_PARAM, id);
}

export function zoneNewHref(): string {
  return hrefWithTarget(DOMAINS_PATH, ZONE_PARAM, NEW_VALUE);
}






