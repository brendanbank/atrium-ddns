/** The route atrium registers at `/atrium-ddns/domains`.
 *
 * Four states kept four, the same table `DeviceBoardPage` keeps and for
 * the same reason:
 *
 * | state | what it means | what it must not be confused with |
 * |---|---|---|
 * | refused | the caller lacks `atrium_ddns.domain.manage` | an empty list |
 * | loading | the request is in flight | an empty list |
 * | failed | the request errored | an empty list |
 * | empty | the tenant genuinely owns no zones | any of the above |
 *
 * *Not measured*, *measured as zero*, *refused* and *never ran* are four
 * states, and rendering them in one type is the single most common way
 * that family arises. Telling a refused user "you have no zones yet"
 * states a fact about their account that is not true.
 *
 * The refusal branch does not fire either query, so a user without the
 * permission does not generate a 403 on every page load.
 */
import { Alert, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { DOMAIN_PERMISSION, domainsQuery, providersQuery } from './api/domains';
import { DdnsRoot } from './host/DdnsRoot';
import { DomainList } from './tenant/DomainList';

export function DomainsInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(DOMAIN_PERMISSION);
  const domains = useQuery(domainsQuery({ enabled: canRead }));
  const providers = useQuery(providersQuery({ enabled: canRead }));

  return (
    <Stack gap="md">
      <Title order={3}>Zones and providers</Title>
      {!canRead ? (
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="domains-refused"
        >
          <Text size="sm">
            Managing zones needs the <code>{DOMAIN_PERMISSION}</code> permission.
            This is a refusal, not an empty list — ask an administrator for the
            permission rather than assuming you own no zones.
          </Text>
        </Alert>
      ) : domains.isLoading || providers.isLoading ? (
        <Text size="sm" data-testid="domains-loading">
          Loading…
        </Text>
      ) : domains.error || providers.error ? (
        <Alert
          color="gray"
          variant="light"
          title="Could not load your zones"
          data-testid="domains-error"
        >
          <Text size="sm" ff="monospace">
            {((domains.error ?? providers.error) as Error).message}
          </Text>
        </Alert>
      ) : domains.data && providers.data ? (
        <DomainList domains={domains.data} providers={providers.data} />
      ) : null}
    </Stack>
  );
}

export function DomainsPage() {
  return (
    <DdnsRoot>
      <DomainsInner />
    </DdnsRoot>
  );
}
