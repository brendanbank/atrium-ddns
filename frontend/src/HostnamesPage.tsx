/** The route atrium registers at `/atrium-ddns/names`.
 *
 * The legacy `/admin/hostnames`, and the surface whose absence made
 * V1M3's signature element unreachable: nothing in the shipped
 * application could construct a `Hostname`, so no tenant could cause a
 * resolution strip to render.
 *
 * Four states kept four, the same table `DeviceBoardPage` and
 * `DomainsPage` keep and for the same reason:
 *
 * | state | what it means | what it must not be confused with |
 * |---|---|---|
 * | refused | the caller lacks `atrium_ddns.hostname.manage` | an empty list |
 * | loading | the requests are in flight | an empty list |
 * | failed | a request errored | an empty list |
 * | empty | the tenant genuinely has no names | any of the above |
 *
 * Telling a refused user "you have no names yet" states a fact about
 * their account that is not true.
 *
 * ## Why three queries and one gate
 *
 * The page needs names, zones and devices: a name must be created inside
 * a zone, and assigned to a device. All three are fetched under the same
 * `canManage` gate rather than each under its own permission, because
 * the *page* is refused or it is not — firing the zone query for a user
 * who cannot see the names it is there to create would generate a 403
 * per page load and render half a form.
 *
 * The permission the page gates on is deliberately the hostname one and
 * not the zone one. A caller who can manage names but not zones sees the
 * page, sees their zones in the select (the zone list is a read the
 * hostname surface needs, and the API gates it on
 * `atrium_ddns.domain.manage`) — so the zone query can still fail on its
 * own, which is why its error is rendered rather than swallowed.
 */
import { Alert, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { devicesQuery } from './api/devices';
import { domainsQuery } from './api/domains';
import { HOSTNAME_PERMISSION, hostnamesQuery } from './api/hostnames';
import { DdnsRoot } from './host/DdnsRoot';
import { HostnameList } from './tenant/HostnameList';

/** This page's path.
 *
 * Defined here rather than in `main.tsx` for the same reason `LOG_PATH`
 * is defined in `LogSearchPage`: the board and the zones page link to
 * it, and importing it from `main.tsx` — which imports every page —
 * would be a cycle. `main.tsx` re-exports it so that file stays the one
 * place a reader looks for "what paths does this bundle own".
 */
export const NAMES_PATH = '/atrium-ddns/names';

export function HostnamesInner() {
  const hasPerm = usePerm();
  const canManage = hasPerm(HOSTNAME_PERMISSION);
  const hostnames = useQuery(hostnamesQuery({ enabled: canManage }));
  const domains = useQuery(domainsQuery({ enabled: canManage }));
  const devices = useQuery(devicesQuery({ enabled: canManage }));

  const loading =
    hostnames.isLoading || domains.isLoading || devices.isLoading;
  const failure = hostnames.error ?? domains.error ?? devices.error;

  return (
    <Stack gap="md">
      <Title order={3}>Names</Title>
      {!canManage ? (
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="hostnames-refused"
        >
          <Text size="sm">
            Managing names needs the <code>{HOSTNAME_PERMISSION}</code>{' '}
            permission. This is a refusal, not an empty list — ask an
            administrator for the permission rather than assuming you have no
            names.
          </Text>
        </Alert>
      ) : loading ? (
        <Text size="sm" data-testid="hostnames-loading">
          Loading…
        </Text>
      ) : failure ? (
        <Alert
          color="gray"
          variant="light"
          title="Could not load your names"
          data-testid="hostnames-error"
        >
          <Text size="sm" ff="monospace">
            {(failure as Error).message}
          </Text>
        </Alert>
      ) : hostnames.data && domains.data && devices.data ? (
        <HostnameList
          hostnames={hostnames.data}
          domains={domains.data}
          devices={devices.data}
        />
      ) : null}
    </Stack>
  );
}

export function HostnamesPage() {
  return (
    <DdnsRoot>
      <HostnamesInner />
    </DdnsRoot>
  );
}
