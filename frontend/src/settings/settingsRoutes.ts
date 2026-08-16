/** The settings group's children, and the routes behind them.
 *
 * `registerSettingsGroup`'s children are **nav-only**: atrium's
 * `/admin/:section` route does not look groups up, so each child's `to`
 * has to point at a path the host registered with `registerRoute`
 * itself. One map, three consumers — the route registrations, the group
 * children, and the pages' own "is there a page for this group" check —
 * so a child pointing at a path no route serves is not expressible.
 *
 * Registration happens at **import time**, before anything can be
 * fetched, so the keys and the sidebar labels are necessarily static
 * here while the fields themselves arrive from
 * `GET /atrium_ddns/config/schema`. The seam between the two is the one
 * thing this file cannot close on its own: a group the server invents
 * and this map does not know has no page, and its fields would render
 * nowhere. `SettingsPage` therefore reads every group out of the served
 * schema and says so, on screen, when it finds one it cannot route to —
 * the same reason the backend keeps an `ungrouped` bucket rather than
 * dropping an unassigned field. Neither side is allowed to lose a
 * setting quietly, because a setting nobody can reach is the entire
 * subject of #73.
 */

/** Group key -> the path the host registers for it. Keys are the
 *  backend's `settings_schema.FIELD_GROUPS` keys. */
export const SETTINGS_ROUTES: Record<string, string> = {
  'rate-limits': '/atrium-ddns/settings/rate-limits',
  'health-checks': '/atrium-ddns/settings/health-checks',
  retention: '/atrium-ddns/settings/retention',
};

/** Sidebar labels. Deliberately the same words as the backend's
 *  `GROUP_LABELS`; the page renders the **server's** label as its
 *  heading, so a divergence shows up rather than hiding. */
export const SETTINGS_LABELS: Record<string, string> = {
  'rate-limits': 'Rate limits',
  'health-checks': 'Health checks',
  retention: 'Retention',
};

/** The registry key for the group itself. */
export const SETTINGS_GROUP_KEY = 'atrium-ddns-settings';

/** Ordered group keys — the order the sidebar and the registrations
 *  use. `Object.keys` on the map, so adding a route adds a child. */
export const SETTINGS_GROUP_KEYS: string[] = Object.keys(SETTINGS_ROUTES);
