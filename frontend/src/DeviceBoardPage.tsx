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
import { Alert, Button, Group, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import {
  useAtriumLocation,
  useAtriumNavigate,
  usePerm,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { BOARD_PERMISSION, boardQuery } from './api/board';
import { DEVICE_PERMISSION } from './api/devices';
import { HOSTNAME_PERMISSION } from './api/hostnames';
import {
  BOARD_ONLY_DEVICE_PARAM,
  BOARD_PATH_HOME,
  DEVICE_PARAM,
  NAME_FOR_PARAM,
  NAME_ID_PARAM,
  NAME_ZONE_PARAM,
  NEW_VALUE,
  boardDeviceHref,
  boardNameNewHref,
  returnFromSearch,
} from './paths';
import { BoardSkeleton } from './board/DeviceBoard';
import { BoardTable } from './board/BoardTable';
import { HealthCheckActions } from './board/HealthCheckActions';
import { DdnsRoot } from './host/DdnsRoot';
import { DeviceCardModal } from './tenant/DeviceCard';
import { DeviceCreateModal } from './tenant/DeviceCreateModal';
import { NameModal } from './tenant/NameModal';


export function DeviceBoardInner() {
  const hasPerm = usePerm();
  const { search } = useAtriumLocation();
  const navigate = useAtriumNavigate();
  const canRead = hasPerm(BOARD_PERMISSION);
  const { data, isLoading, error } = useQuery(boardQuery({ enabled: canRead }));
  /** Both modals are the address bar, the way the zone modal already is.
   *
   *  They were `useState`, so a reload or a pasted link landed on the bare
   *  board — and the name link went to `/atrium-ddns/names`, a whole page
   *  away, because the board could not show a name itself.
   *
   *  Reading them from the URL also buys the return behaviour without a
   *  stack: opening a name *from* a device card carries `?from=` naming
   *  the card's own address, so closing the name goes back to the device.
   *  It survives a reload, which a component-held stack would not. */
  const params = new URLSearchParams(search);
  const rawDevice = params.get(DEVICE_PARAM);
  const openDevice =
    rawDevice !== null && /^\d+$/.test(rawDevice) ? Number(rawDevice) : null;
  /** `?device=new` is the create form, not a card with no device. */
  const creatingDevice = rawDevice === NEW_VALUE;
  const rawName = params.get(NAME_ID_PARAM);
  const nameOpen = rawName !== null;
  const nameId =
    rawName === null || rawName === NEW_VALUE ? null : Number(rawName) || null;
  const rawFor = params.get(NAME_FOR_PARAM);
  const presetDeviceId =
    rawFor !== null && /^\d+$/.test(rawFor) ? Number(rawFor) : null;
  /** Where closing a modal goes. `?from=` when one modal opened another,
   *  otherwise the bare board. */
  const closeTo = returnFromSearch(search) ?? BOARD_PATH_HOME;
  /** `?zone=<name>` focuses the board on one zone. The zones page links
   *  here that way now: it used to send you to `/atrium-ddns/names`, a
   *  surface that is going away. Read once, into the table's own filter
   *  state — after that it is a control, not an address. */
  const zoneFromUrl = params.get(NAME_ZONE_PARAM);
  /** `?onlyDevice=` narrows the table to one device. Distinct from
   *  `?device=`, which opens that device's card — the card links here to
   *  show the rows it no longer draws itself. */
  const onlyDeviceFromUrl = params.get(BOARD_ONLY_DEVICE_PARAM);

  return (
    <Stack gap="md">
      <Group justify="space-between" align="baseline">
        <Title order={3}>Devices and names</Title>
        {/* The two things you come here to *do*, and the reason they are
            buttons rather than a link in the corner.

            #69 found that the board, the zones page and the log all
            *describe* hostnames while none of them could create one, so
            the object this page renders was unreachable from the page
            that renders it. The separate Devices and Names nav items were
            then removed, on the grounds that this board is both of those
            lists joined — true for reading them, and false for making
            one. What was left said *"You have no devices yet. Add one to
            get a DDNS username and password"* over a page with no way to
            add one: an instruction the surface carrying it cannot follow.

            Each is gated on the permission for the thing it creates, not
            on the board's: an action that answers a refusal is worse than
            no action. `Manage names` stays as the way to the list, since
            browsing names and adding one are different errands.

            Plain anchors for the reason `LogLink` gives — this tree is
            mounted inside atrium's React, so react-router's `Link` is not
            reachable and a bare `pushState` would move the address bar
            without telling the router. `component="a"` keeps that while
            still rendering as a button. */}
        <Group gap="sm" align="center">
          {hasPerm(DEVICE_PERMISSION) ? (
            <Button
              component="a"
              href={`${BOARD_PATH_HOME}?${DEVICE_PARAM}=${NEW_VALUE}`}
              size="xs"
              data-testid="board-add-device"
            >
              Add a device
            </Button>
          ) : null}
          {hasPerm(HOSTNAME_PERMISSION) ? (
            <Button
              component="a"
              href={boardNameNewHref()}
              size="xs"
              variant="default"
              data-testid="board-add-name"
            >
              Add a name
            </Button>
          ) : null}
        </Group>
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
          <BoardTable
            board={data}
            onOpenDevice={(id) => navigate(boardDeviceHref(id))}
            initialZoneFilter={zoneFromUrl}
            initialDeviceFilter={onlyDeviceFromUrl}
          />
          {/* §18.2 — the board's device name is now a way in, and this
              is where it goes. The same `DeviceCard` the route renders
              and the device list opens: one definition, asserted by
              module identity in `src/test/sharedCard.test.tsx`. */}
          <DeviceCardModal
            deviceId={openDevice}
            onClose={() => navigate(closeTo, { replace: true })}
          />
          <DeviceCreateModal
            opened={creatingDevice}
            onClose={() => navigate(closeTo, { replace: true })}
          />
          <NameModal
            nameId={nameId}
            opened={nameOpen}
            presetDeviceId={presetDeviceId}
            onClose={() => navigate(closeTo, { replace: true })}
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
