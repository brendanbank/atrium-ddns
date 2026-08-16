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
  IconKey,
  IconListSearch,
  IconRouter,
  IconTag,
  IconWorld,
} from '@tabler/icons-react';
import {
  type AtriumRegistry,
  makeWrapperElement,
} from '@brendanbank/atrium-host-bundle-utils';

import { AtriumDdnsAdminTab } from './AtriumDdnsAdminTab';
import { AtriumDdnsPage } from './AtriumDdnsPage';
import { AtriumDdnsProfileItem } from './AtriumDdnsProfileItem';
import { AtriumDdnsWidget } from './AtriumDdnsWidget';
import { DeviceBoardPage } from './DeviceBoardPage';
import { DeviceDetailPage } from './DeviceDetailPage';
import { DevicesPage } from './DevicesPage';
import { DomainsPage } from './DomainsPage';
import { HELP_PATH, HelpPage } from './HelpPage';
import { NAMES_PATH, HostnamesPage } from './HostnamesPage';
import { LOG_PATH, LogSearchPage } from './LogSearchPage';
import { SettingsPage } from './SettingsPage';
import { ZoneDetailPage } from './ZoneDetailPage';
import { CONFIG_PERMISSION } from './api/config';
import {
  BOARD_PATH,
  DEVICES_PATH,
  DEVICE_DETAIL_PATH,
  DOMAINS_PATH,
  ZONE_ROUTE_PATH,
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
export { BOARD_PATH, DEVICES_PATH, DOMAINS_PATH };

/** The two parameterised detail routes — #88's `/atrium-ddns/zones/:id`
 *  and #89's `/atrium-ddns/devices/:id`. Re-exported for the same reason
 *  as their three neighbours above: this file stays the one place a
 *  reader looks for "what paths does this bundle own". */
export { ZONE_ROUTE_PATH, DEVICE_DETAIL_PATH };

/** #46's log search. Defined in `LogSearchPage` because `logHref` needs
 *  it too, and re-exported here for the same reason. */
export { LOG_PATH };

/** #69's hostname lifecycle — the legacy `/admin/hostnames`. Defined in
 *  `HostnamesPage` because the board and the zones page link to it. */
export { NAMES_PATH };

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
  reg.registerHomeWidget({
    key: 'atrium-ddns-widget',
    render: () => makeWrapperElement(<AtriumDdnsWidget />),
  });
  reg.registerRoute({
    key: 'atrium-ddns-page',
    path: '/atrium-ddns',
    render: () => makeWrapperElement(<AtriumDdnsPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-nav',
    label: 'Atrium Ddns',
    to: '/atrium-ddns',
    icon: AtriumReact.createElement(IconHandStop, { size: 18 }),
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
  reg.registerNavItem({
    key: 'atrium-ddns-board-nav',
    label: 'Devices and names',
    to: BOARD_PATH,
    icon: AtriumReact.createElement(IconRouter, { size: 18 }),
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
  // #88's zone detail — design Part II §10.2, and §12 for why it is a
  // route rather than a drawer: a resolution strip needs 592px, atrium's
  // shell gives 1168px, a 360/800 split leaves ~790px and Mantine's `lg`
  // drawer is 620px. The drawer is the one below the one-strip minimum,
  // so the signature element would wrap inside its own detail view.
  //
  // **Route, no nav item.** The other five surfaces are destinations;
  // this one is reached from a row. A sidebar entry pointing at
  // `/atrium-ddns/zones/:id` would be a link to a literal colon, which
  // is the "a writer nothing calls" shape wearing a nav item's clothing.
  //
  // The path carries react-router's `:id`, which works because atrium
  // drops a registered path straight into `<Route path=…>` (App.tsx).
  // The host subtree cannot read it back through `useParams` — a second
  // React root, no shared context — so the page parses the pathname via
  // `useAtriumLocation`.
  reg.registerRoute({
    key: 'atrium-ddns-zone-detail',
    path: ZONE_ROUTE_PATH,
    render: () => makeWrapperElement(<ZoneDetailPage />),
  });
  reg.registerRoute({
    key: 'atrium-ddns-devices',
    path: DEVICES_PATH,
    render: () => makeWrapperElement(<DevicesPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-devices-nav',
    label: 'Devices',
    to: DEVICES_PATH,
    icon: AtriumReact.createElement(IconKey, { size: 18 }),
  });
  // #89's device detail — ui-design.md §11.2. A **route** and not a
  // drawer, decided on §12's measured width budget: one resolution
  // strip needs ≈592px, atrium's shell gives 1168px, and Mantine's `lg`
  // drawer at 620px is below the one-strip minimum, so the signature
  // element would wrap inside its own detail view.
  //
  // Registered with a route and deliberately **no nav item**: it is a
  // destination reached from a device row, not a place in the sidebar,
  // and a nav item pointing at a literal `:id` would be a dead link.
  // §12: "the list rows stay as they are … this adds a destination, it
  // does not redraw the list."
  //
  // The path carries react-router's `:id` because atrium's `App.tsx`
  // hands every registered `path` straight to `<Route path=…>`. It is
  // registered *after* `DEVICES_PATH`, which is not a subtlety
  // react-router cares about — its ranked matcher prefers the static
  // segment regardless — but the order reads the way the URLs nest.
  reg.registerRoute({
    key: 'atrium-ddns-device-detail',
    path: DEVICE_DETAIL_PATH,
    render: () => makeWrapperElement(<DeviceDetailPage />),
  });
  // #69's hostname lifecycle — the legacy `/admin/hostnames`, and the
  // registration whose absence made the resolution strip unreachable:
  // the board, the zones page and the log all describe an object nothing
  // in the bundle could create. Gated the same way as its neighbours —
  // no `perm` on the nav item, because the page's own gate renders a
  // refusal rather than an empty list.
  reg.registerRoute({
    key: 'atrium-ddns-names',
    path: NAMES_PATH,
    render: () => makeWrapperElement(<HostnamesPage />),
  });
  reg.registerNavItem({
    key: 'atrium-ddns-names-nav',
    label: 'Names',
    to: NAMES_PATH,
    icon: AtriumReact.createElement(IconTag, { size: 18 }),
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
