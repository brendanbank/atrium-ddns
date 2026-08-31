/** The route atrium registers at `/atrium-ddns/board` — the primary surface.
 *
 * Four states, and keeping them four is the point of the component:
 *
 * | state | what it means | what it must not be confused with |
 * |---|---|---|
 * | refused | the caller lacks `atrium_ddns.device.manage` | an empty board |
 * | loading | the request is in flight | an empty board |
 * | failed | the request errored | an empty board |
 * | empty | the tenant genuinely owns nothing | any of the above |
 *
 * That table is `docs/ops/overnight-template.md`'s "`n/a` is never `0`"
 * rule wearing a UI's clothes. *Not measured*, *measured as zero*,
 * *refused* and *never ran* are four states, and rendering them in one
 * type is the single most common way the family arises. A board that
 * answered "You have no devices yet" to a user who was refused would be
 * telling them a fact about their account that is not true.
 *
 * The refusal branch also does not fire the query at all — `enabled` is
 * the permission — so a user without it does not generate a 403 on every
 * page load.
 */
import { useState } from 'react';
import { Alert, Anchor, Group, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { BOARD_PERMISSION, boardQuery } from './api/board';
import { HOSTNAME_PERMISSION } from './api/hostnames';
import { BoardSkeleton } from './board/DeviceBoard';
import { BoardTable } from './board/BoardTable';
import { HealthCheckActions } from './board/HealthCheckActions';
import { NAMES_PATH } from './HostnamesPage';
import { DdnsRoot } from './host/DdnsRoot';
import { DeviceCardModal } from './tenant/DeviceCard';

export function DeviceBoardInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(BOARD_PERMISSION);
  const { data, isLoading, error } = useQuery(boardQuery({ enabled: canRead }));
  /** Which device's card is open. Held here rather than in
   *  `DeviceBoard` because `DeviceCard` imports `HostnameBlock` from
   *  that module, and importing the card there would close a cycle. */
  const [openDevice, setOpenDevice] = useState<number | null>(null);

  return (
    <Stack gap="md">
      <Group justify="space-between" align="baseline">
        <Title order={3}>Devices and names</Title>
        {/* The way out of an empty board. #69 found that the board, the
            zones page and the log all *describe* hostnames and none of
            them could create one — so the object this page renders was
            unreachable from the page that renders it. Gated on the
            hostname permission rather than the board's: a link to a page
            that answers a refusal is worse than no link.

            A plain anchor for the reason `LogLink` gives — this tree is
            mounted inside atrium's React, so react-router's `Link` is
            not reachable and a bare `pushState` would move the address
            bar without telling the router. */}
        {hasPerm(HOSTNAME_PERMISSION) ? (
          <Anchor href={NAMES_PATH} size="sm" data-testid="board-names-link">
            Manage names
          </Anchor>
        ) : null}
      </Group>
      {!canRead ? (
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="board-refused"
        >
          <Text size="sm">
            Reading the device board needs the{' '}
            <code>{BOARD_PERMISSION}</code> permission. This is a refusal,
            not an empty board — ask an administrator for the permission
            rather than assuming you have no devices.
          </Text>
        </Alert>
      ) : isLoading ? (
        <BoardSkeleton />
      ) : error ? (
        <Alert color="gray" variant="light" title="Could not load the board" data-testid="board-error">
          {/* Diagnostics in full: the status and the server's own words.
              Redact secrets, never diagnostics. */}
          <Text size="sm" ff="monospace">
            {(error as Error).message}
          </Text>
        </Alert>
      ) : data ? (
        <>
          {/* #75's two on-demand actions. Rendered only in the `data`
              branch, and that is not a layout choice: the cadence
              sentence quotes `health_check_interval_minutes` out of the
              board payload, so there is no reading of it to render
              before the payload arrives. A button placed in the loading
              or refused branch would either invent that number or print
              a blank where it goes. */}
          <HealthCheckActions
            intervalMinutes={data.health_check_interval_minutes}
          />
          <BoardTable board={data} onOpenDevice={setOpenDevice} />
          {/* §18.2 — the board's device name is now a way in, and this
              is where it goes. The same `DeviceCard` the route renders
              and the device list opens: one definition, asserted by
              module identity in `src/test/sharedCard.test.tsx`. */}
          <DeviceCardModal
            deviceId={openDevice}
            onClose={() => setOpenDevice(null)}
          />
        </>
      ) : null}
    </Stack>
  );
}

export function DeviceBoardPage() {
  return (
    <DdnsRoot>
      <DeviceBoardInner />
    </DdnsRoot>
  );
}
