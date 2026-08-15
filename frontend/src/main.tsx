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
  IconHandStop,
  IconKey,
  IconListSearch,
  IconRouter,
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
import { DevicesPage } from './DevicesPage';
import { DomainsPage } from './DomainsPage';
import { LOG_PATH, LogSearchPage } from './LogSearchPage';

/** The board's path. Exported so a test asserts the registered route
 *  against the same string the nav item points at, rather than against
 *  a literal typed twice. */
export const BOARD_PATH = '/atrium-ddns/board';

/** #45's two tenant surfaces, same rule. */
export const DOMAINS_PATH = '/atrium-ddns/domains';
export const DEVICES_PATH = '/atrium-ddns/devices';

/** #46's log search. Defined in `LogSearchPage` because `logHref` needs
 *  it too, and re-exported here so this file stays the one place a
 *  reader looks for "what paths does this bundle own". */
export { LOG_PATH };

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
