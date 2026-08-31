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
import {
  useAtriumLocation,
  useAtriumNavigate,
  usePerm,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { DEVICE_PERMISSION, devicesQuery } from './api/devices';
import { DdnsRoot } from './host/DdnsRoot';
import { DeviceList } from './tenant/DeviceList';
import { DeviceCardModal } from './tenant/DeviceCard';
import { DEVICES_PATH, deviceFromSearch, deviceHrefParam } from './paths';

export function DevicesInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(DEVICE_PERMISSION);
  const { search } = useAtriumLocation();
  const navigate = useAtriumNavigate();
  /** The open device, read from `?device=`. Never mirrored into
   *  `useState`: the modal used to live in `DeviceList`'s state, so a
   *  reload — or a pasted link, or Back — landed on the bare list. The
   *  address is the only thing that knows, so nothing can disagree with
   *  it. Same helper the zones page uses. */
  const target = deviceFromSearch(search);

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
        <DeviceList
          devices={data}
          onOpen={(id) => navigate(deviceHrefParam(id))}
        />
      ) : null}

      {/* Open-ness is the address bar. `target.id` is `null` only on a
          `?device=new`, which this surface does not issue — creation has
          its own modal in `DeviceList` because it returns a secret. */}
      <DeviceCardModal
        deviceId={target.open ? target.id : null}
        onClose={() => navigate(DEVICES_PATH, { replace: true })}
      />
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
