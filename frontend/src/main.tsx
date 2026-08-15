/** Host bundle entry.
 *
 * Atrium loads this module via `import(system.host_bundle_url)` after
 * the SPA boots. The import-time side-effects below populate the
 * registry. The dual-tree mount pattern (atrium-React owns the
 * wrapper, the host's React owns the subtree) is encapsulated in
 * `makeWrapperElement` from `@brendanbank/atrium-host-bundle-utils`.
 */
import { IconHandStop } from '@tabler/icons-react';
import {
  type AtriumRegistry,
  makeWrapperElement,
} from '@brendanbank/atrium-host-bundle-utils';

import { AtriumDdnsAdminTab } from './AtriumDdnsAdminTab';
import { AtriumDdnsPage } from './AtriumDdnsPage';
import { AtriumDdnsProfileItem } from './AtriumDdnsProfileItem';
import { AtriumDdnsWidget } from './AtriumDdnsWidget';

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
