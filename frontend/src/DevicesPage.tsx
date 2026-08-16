/** The route atrium registers at `/atrium-ddns/devices`.
 *
 * The same four-state table as `DomainsPage` and `DeviceBoardPage`, on
 * the same permission the board reads under — a user who can see a
 * device on the board must be able to find the page that created it,
 * and gating the two differently would produce a board full of devices
 * whose credentials the reader cannot manage.
 */
import { Alert, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { DEVICE_PERMISSION, devicesQuery } from './api/devices';
import { DdnsRoot } from './host/DdnsRoot';
import { DeviceList } from './tenant/DeviceList';

export function DevicesInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(DEVICE_PERMISSION);
  const { data, isLoading, error } = useQuery(
    devicesQuery({ enabled: canRead }),
  );

  return (
    <Stack gap="md">
      <Title order={3}>Devices</Title>
      {!canRead ? (
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="devices-refused"
        >
          <Text size="sm">
            Managing devices needs the <code>{DEVICE_PERMISSION}</code>{' '}
            permission. This is a refusal, not an empty list — ask an
            administrator for the permission rather than assuming you have no
            devices.
          </Text>
        </Alert>
      ) : isLoading ? (
        <Text size="sm" data-testid="devices-loading">
          Loading…
        </Text>
      ) : error ? (
        <Alert
          color="gray"
          variant="light"
          title="Could not load your devices"
          data-testid="devices-error"
        >
          <Text size="sm" ff="monospace">
            {(error as Error).message}
          </Text>
        </Alert>
      ) : data ? (
        <DeviceList devices={data} />
      ) : null}
    </Stack>
  );
}

export function DevicesPage() {
  return (
    <DdnsRoot>
      <DevicesInner />
    </DdnsRoot>
  );
}
