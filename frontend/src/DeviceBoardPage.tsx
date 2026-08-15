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
import { Alert, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { BOARD_PERMISSION, boardQuery } from './api/board';
import { BoardSkeleton, DeviceBoard } from './board/DeviceBoard';
import { DdnsRoot } from './host/DdnsRoot';

export function DeviceBoardInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(BOARD_PERMISSION);
  const { data, isLoading, error } = useQuery(boardQuery({ enabled: canRead }));

  return (
    <Stack gap="md">
      <Title order={3}>Devices and names</Title>
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
        <DeviceBoard board={data} />
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
