/** Host bundle entry.
 *
 * Atrium loads this module via `import(system.host_bundle_url)` after
 * the SPA boots. The import-time side-effects below populate the
 * registry. The dual-tree mount pattern (atrium-React owns the
 * wrapper, the host's React owns the subtree) is encapsulated in
 * `makeWrapperElement` from `@brendanbank/atrium-host-bundle-utils`.
 *
 * **The pattern to copy.** Every entry below is
 * `makeWrapperElement(<Something />)` where `Something` mounts inside
 * `<DdnsRoot>` — the wrapper that owns the MantineProvider +
 * QueryClientProvider + AtriumProvider stack and the three Mantine props
 * that stop a nested provider restyling atrium's shell. Issues #45 and
 * #46 add their registrations here in the same shape; nothing new should
 * spell the provider stack out again.
 */
import {
  IconAdjustments,
  IconHandStop,
  IconHelp,
  IconListSearch,
  IconRouter,
  IconWorld,
} from '@tabler/icons-react';
import {
  type AtriumRegistry,
  makeWrapperElement,
} from '@brendanbank/atrium-host-bundle-utils';

import { AtriumDdnsAdminTab } from './AtriumDdnsAdminTab';
import { AtriumDdnsProfileItem } from './AtriumDdnsProfileItem';
import { DeviceBoardPage } from './DeviceBoardPage';
import { DomainsPage } from './DomainsPage';
import { HELP_PATH, HelpPage } from './HelpPage';
import { LOG_PATH, LogSearchPage } from './LogSearchPage';
import { SettingsPage } from './SettingsPage';
import { CONFIG_PERMISSION } from './api/config';
import {
  BOARD_PATH,
  DOMAINS_PATH,
} from './paths';
import {
  SETTINGS_GROUP_KEY,
  SETTINGS_GROUP_KEYS,
  SETTINGS_LABELS,
  SETTINGS_ROUTES,
} from './settings/settingsRoutes';

/** The three paths this file used to declare. They moved to `paths.ts`
 *  when #75's help page needed to name them: `main` imports `HelpPage`,
 *  so `HelpPage` importing `main` would be a cycle. Re-exported
 *  unchanged, so this file stays the one place a reader looks for
 *  "what paths does this bundle own". */
export { BOARD_PATH, DOMAINS_PATH };

/** The two parameterised detail routes — #88's `/atrium-ddns/zones/:id`
 *  and #89's `/atrium-ddns/devices/:id`. Re-exported for the same reason
 *  as their three neighbours above: this file stays the one place a
 *  reader looks for "what paths does this bundle own". */

/** #46's log search. Defined in `LogSearchPage` because `logHref` needs
 *  it too, and re-exported here for the same reason. */
export { LOG_PATH };

/** #69's hostname lifecycle — the legacy `/admin/hostnames`. Defined in
 *  `HostnamesPage` because the board and the zones page link to it. */

/** #75's help entry — the legacy `/admin/help`, which swept to a
 *  negative across both deployed bundles. */
export { HELP_PATH };

const reg = window.__ATRIUM_REGISTRY__ as AtriumRegistry | undefined;
const AtriumReact = window.React;

if (!reg || !AtriumReact) {
  console.error(
    '[atrium-ddns] window.__ATRIUM_REGISTRY__ or window.React missing — atrium SPA must mount before the host bundle loads',
  );
} else {
  // Atrium's `/` renders `HomePage`, which renders host home widgets
  // and hides its own greeting once any host registers one. So this
  // widget IS how a host replaces the landing page — there is no
  // setting for it, and nothing a backend rebuild would change.
  reg.registerHomeWidget({
    key: 'atrium-ddns-widget',
    render: () => makeWrapperElement(<DeviceBoardPage />),
    // `full` so the board owns the width. Atrium wraps a widget in a
    // 680px Container by default, and the table needs the shell's
    // whole 1168px — see `ui-design.md` §3.
    width: 'full',
  });
  // The root **is** the board. It was a scaffold landing page, so
  // the surface the product exists for was one click in, and the
  // first thing a tenant saw was a page about the app rather than
  // about their names.
  reg.registerRoute({
    key: 'atrium-ddns-page',
    path: '/atrium-ddns',
    render: () => makeWrapperElement(<DeviceBoardPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-board-nav',
    // Named for what it shows, not for the app: it is the board now,
    // and "Atrium Ddns" inside Atrium Ddns told a reader nothing. One
    // entry, not three — the separate Devices and Names items are gone,
    // because the board is both of those lists joined.
    // The bundle root, which renders the board. `BOARD_PATH` still
    // resolves and renders the same page — it is simply not where the
    // sidebar sends you, because the board is the landing surface and a
    // sidebar entry pointing one level deeper than the home page makes
    // two addresses for one place and highlights neither.
    label: 'Devices and names',
    to: '/atrium-ddns',
    icon: AtriumReact.createElement(IconRouter, { size: 18 }),
  });
  // The primary surface. Deliberately not permission-gated at the
  // registry level: `registerNavItem` has no `perm`, and the page's own
  // gate renders a *refusal* rather than an empty board — which is the
  // distinction the whole surface is built around. Hiding the nav item
  // would turn "you may not read this" into "this does not exist".
  reg.registerRoute({
    key: 'atrium-ddns-board',
    path: BOARD_PATH,
    render: () => makeWrapperElement(<DeviceBoardPage />),
  });
    // #45's tenant CRUD, registered in the same shape and gated the same
  // way: no `perm` on the nav item, because each page's own gate
  // renders a *refusal* rather than an empty list. Hiding the nav item
  // would turn "you may not manage these" into "these do not exist".
  reg.registerRoute({
    key: 'atrium-ddns-domains',
    path: DOMAINS_PATH,
    render: () => makeWrapperElement(<DomainsPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-domains-nav',
    label: 'Zones and providers',
    to: DOMAINS_PATH,
    icon: AtriumReact.createElement(IconWorld, { size: 18 }),
  });
  // #46's log search. Deliberately **not** permission-gated at the
  // registry level and deliberately carrying no `perm` — but for a
  // different reason from the three above, and the difference matters.
  // Those render a refusal because they *are* gated. This one is not
  // gated at all: any authenticated caller may read their own log,
  // because it is their own data. `atrium_ddns.events.read.all` widens
  // the query to every tenant and does nothing else — #14 asserted
  // those are different reaches, and a `perm` here would collapse them
  // by hiding the surface from the people it was built for.
  reg.registerRoute({
    key: 'atrium-ddns-logs',
    path: LOG_PATH,
    render: () => makeWrapperElement(<LogSearchPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-logs-nav',
    label: 'Log search',
    to: LOG_PATH,
    icon: AtriumReact.createElement(IconListSearch, { size: 18 }),
  });
  // #75's help entry — the legacy `GET /admin/help`, which #47 swept to
  // a negative across both deployed bundles: zero help surfaces
  // anywhere, host side or shell side. Registered with a nav item
  // rather than only a route, because a help page nothing links to is
  // the same absence one indirection further along.
  //
  // No `perm`, and unlike its neighbours that is not a refusal-versus-
  // empty argument: this page reads no tenant data and calls no
  // endpoint. It describes the surfaces and links the documentation, so
  // there is nothing here to be refused.
  //
  // `order` puts it last in the sidebar. The five items above are the
  // work; help is where you go when one of them did not do what you
  // expected.
  reg.registerRoute({
    key: 'atrium-ddns-help',
    path: HELP_PATH,
    render: () => makeWrapperElement(<HelpPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-help-nav',
    label: 'Help',
    to: HELP_PATH,
    icon: AtriumReact.createElement(IconHelp, { size: 18 }),
    order: 900,
  });
  // #73 — plan §4's one unbuilt clause: "Rate limits, health-check
  // config, retention become one nested group via
  // `registerSettingsGroup` rather than sibling pages."
  //
  // The routes come first and are registered unconditionally. A
  // `SettingsGroup` child is nav-only — atrium's `/admin/:section` route
  // does not look groups up — so the child's `to` must be a path this
  // bundle serves, and a route registered only when the group is
  // registered would make the two ways of arriving here disagree.
  //
  // The pages are not perm-gated at the registry level, for the same
  // reason the tenant pages are not: each renders an explicit *refusal*
  // for a caller without `app_setting.manage`. The group below IS
  // perm-gated, because that is a sidebar entry rather than a page —
  // hiding a nav item a user cannot use is not the same as answering
  // "this does not exist" to a URL they typed.
  for (const key of SETTINGS_GROUP_KEYS) {
    reg.registerRoute({
      key: `atrium-ddns-settings-${key}`,
      path: SETTINGS_ROUTES[key],
      render: () => makeWrapperElement(<SettingsPage groupKey={key} />),
    });
  }
  if (reg.registerSettingsGroup) {
    reg.registerSettingsGroup({
      key: SETTINGS_GROUP_KEY,
      label: 'DDNS configuration',
      icon: AtriumReact.createElement(IconAdjustments, { size: 18 }),
      // Atrium's own config sections (System, Auth, Branding) live in
      // the admin bucket and gate on this same permission. Sitting
      // beside them is the point: the settings are atrium's
      // `app_settings` rows, edited through atrium's own endpoint.
      section: 'admin',
      perm: CONFIG_PERMISSION,
      // After the built-ins, which occupy 100..900.
      order: 950,
      children: SETTINGS_GROUP_KEYS.map((key) => ({
        key,
        label: SETTINGS_LABELS[key],
        to: SETTINGS_ROUTES[key],
      })),
    });
  } else {
    // Available since atrium 0.25 and typed optional on the registry,
    // so this branch is reachable on an older image. Say what was lost
    // and what still works: the pages are registered above and remain
    // reachable by URL — silence here would reproduce #73 exactly, a
    // surface that exists and nothing names.
    console.error(
      '[atrium-ddns] this atrium build has no registerSettingsGroup ' +
        '(added in 0.25), so the DDNS configuration group will not appear ' +
        'in the admin sidebar. The pages are still served at ' +
        Object.values(SETTINGS_ROUTES).join(', '),
    );
  }
  reg.registerAdminTab({
    key: 'atrium-ddns',
    label: 'Atrium Ddns',
    icon: AtriumReact.createElement(IconHandStop, { size: 14 }),
    perm: 'atrium_ddns.write',
    render: () => makeWrapperElement(<AtriumDdnsAdminTab />),
  });
  reg.registerProfileItem({
    key: 'atrium-ddns-profile',
    slot: 'after-roles',
    render: () => makeWrapperElement(<AtriumDdnsProfileItem />),
  });
}
