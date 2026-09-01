/** The device card — one component, three call sites, one definition.
 *
 * `docs/ops/ui-design.md` **Part III §17**. The operator asked twice for
 * a modal; §12's route is kept because linkability and Back survived on
 * their own merits. So this component is rendered by
 * `/atrium-ddns/devices/:id` (`DeviceDetailPage`), by the device list's
 * row (`DeviceList`) and by the board's row (`DeviceBoardPage`) — the
 * last two through `DeviceCardModal` below. `src/test/sharedCard.test.tsx`
 * asserts the sharing by module identity rather than by inspection: it
 * substitutes this module and checks the substitute reaches every
 * entrance, which a private copy at any of them would fail.
 *
 * ## The three things this card must not get wrong
 *
 * **1. The rate limit's third state.** `null` is a *value* on that field
 * and means *inherit the installation default*. It is offered as an
 * explicit choice — a radio the operator selects — rather than being
 * what happens when the box is left empty, because #73's `DeviceUpdateIn`
 * docstring records why conflating *omitted* with *null* silently
 * un-mutes a device somebody muted on purpose.
 *
 * **2. The installation default is only knowable when it is inherited.**
 * `effective_rate_limit_per_minute` is the resolved number; when
 * `rate_limit_per_minute` is `null` the two are the same and the
 * installation default can be named. When a per-device value is set,
 * this browser **cannot** know the default — it lives behind
 * `app_setting.manage`, which a plain tenant does not hold — so the
 * radio says "inherit the installation default" with no number rather
 * than a number it would have had to invent.
 *
 * **3. Rotation is not on the Save button.** It has its own control,
 * below a rule, behind its own confirmation, and the consequence is
 * stated before the button rather than after: a router still configured
 * with the old secret starts answering `badauth` the moment the request
 * commits, and telling someone afterwards is telling them during the
 * outage.
 *
 * ## Why the name is still edited in place
 *
 * It is one short string with one failure mode, and the pencil swaps the
 * heading for an input at the same position. That decision is unchanged
 * by §17: §17 is about how the *card* is reached, not about how a field
 * inside it is edited, and putting a second modal over the first to
 * rename would hide the thing being renamed behind two overlays.
 */
import { useState } from 'react';
import {
  Alert,
  Anchor,
  Button,
  Divider,
  Group,
  Modal,
  NumberInput,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { BOARD_QUERY_KEY } from '../api/board';
import {
  DEVICES_QUERY_KEY,
  DEVICE_PERMISSION,
  deleteDevice,
  deviceQuery,
  renameDevice,
  rotateDeviceSecret,
  type Device,
  type DeviceSecret,
} from '../api/devices';
import { ApiError } from '../api/http';
import { boardForDeviceHref } from '../paths';
import { absoluteTitle, formatAge } from '../board/format';
import { CARD_MODAL_PADDING_PX, CARD_MODAL_WIDTH_PX, CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { MigratedNotice, SecretOnceModal } from './SecretOnce';

/** The server's words, verbatim.
 *
 * `ApiError.body` is the response body; `message` is the wrapper. The
 * 409 this form exists to render carries the offending name in
 * `detail`, so the JSON is unwrapped when it parses and the raw text is
 * shown when it does not — never a message this file wrote instead.
 * Redact secrets, never diagnostics.
 */
export function refusalText(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
      if (parsed.detail !== undefined) return JSON.stringify(parsed.detail);
    } catch {
      /* not JSON — fall through to the raw body */
    }
    return error.body || error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** The device's editable settings, as one form with one Save.
 *
 * They used to be two fields with a Save button each, which made the card
 * disagree with itself about what "saving" meant: two requests, two
 * refusal slots, and a modal you left by the `×` because nothing in it
 * was the way out. Editing the name and the limit together took two round
 * trips and could half-succeed — the rename landing while the limit was
 * refused, with no single place saying so.
 *
 * One form, one PATCH. `renameDevice` already sends both keys, because
 * `rate_limit_per_minute` is required on that route and `null` is a value
 * on the column rather than an omission (#73) — so the single request was
 * always the shape the API wanted.
 */
export interface DeviceCardProps {
  /** The device this card is about. Parsed from the pathname by the
   *  route, taken from the row by either modal. */
  deviceId: number;
  /** Closes the surface showing this card, for Cancel. Optional because
   *  the *route* renders this card too and has nothing to close — there
   *  Cancel reverts the draft instead, which is the same promise on a
   *  surface that cannot go away. */
  onClose?: () => void;
  /** Called after the device is deleted, so whatever is showing this card
   *  can stop. Optional because the *route* renders this card too, and a
   *  route has nothing to close — it shows "no such device" instead,
   *  which is the truth once the row is gone. */
  onDeleted?: () => void;
}

export function DeviceCard({ deviceId, onClose, onDeleted }: DeviceCardProps) {
  const client = useQueryClient();
  const hasPerm = usePerm();
  const canRead = hasPerm(DEVICE_PERMISSION);

  const { data, isLoading, error } = useQuery(
    deviceQuery(deviceId, { enabled: canRead }),
  );
  // The board query used to live here, to draw this device's strips.
  // The card links to the board instead of redrawing it, so the second
  // request is gone with the copy it fed — one fewer fetch per card, and
  // one fewer rendering to keep in step.

  // The secret lives here and nowhere else — not in the query cache,
  // not in storage, not in a ref that survives a remount.
  const [issued, setIssued] = useState<DeviceSecret | null>(null);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [rotateError, setRotateError] = useState<string | null>(null);

  const refresh = (next: Device) => {
    client.setQueryData([...DEVICES_QUERY_KEY, next.id], next);
    // The list and the board both show this device, and both are now
    // one rename behind.
    // The board is the landing page and draws the same devices and
    // names. Invalidating only the list a surface happens to own leaves
    // the board showing rows that no longer exist — which is what a
    // create flow returning to the board looked like: a new device that
    // was not there until a manual reload.
    void client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
    void client.invalidateQueries({ queryKey: BOARD_QUERY_KEY });
  };

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // --- the editable settings, one form with one Save -------------------
  //
  // Lifted out of a `DeviceSettings` child so its Save and Cancel can be
  // drawn at the *bottom* of the card beside Rotate. They were mid-modal,
  // above the username and the names list — a set of verbs with unrelated
  // read-only detail below them, which reads as the end of the card when
  // it is the middle.
  //
  // Seeded in the render phase rather than by `useState(data.name)`,
  // because `data` arrives from a query and a `useState` initialiser runs
  // once, before it does. Keyed on the device's identity so reopening the
  // card on a different device re-seeds and a re-render does not — the
  // same pattern `ZoneModal` and `NameModal` use, and the same bug it was
  // written for: a form that opened blank on a row that plainly had one.
  const [seededFrom, setSeededFrom] = useState<number | undefined>(undefined);
  const [name, setName] = useState('');
  /* The limit box is **prefilled with the limit actually in force** — the
     per-device value if there is one, otherwise the installation default
     the server reports. So it always opens showing the number the device
     is really held to, which is what you came to read.

     **Clearing the box is how you go back to inheriting.** Empty is the
     only way `null` still reaches the API, and `null` is a value on that
     column rather than an omission (#73). Without that mapping a device,
     once opened, could never inherit again. */
  const [limit, setLimit] = useState<number | ''>('');
  /* Whether the box has actually been typed in, and it is load-bearing.
     Prefilling with the *effective* limit was safe while the limit had a
     Save of its own; with one Save for the card, merely renaming would
     otherwise send `30` and silently pin an inheriting device to today's
     installation default — a rename that quietly stops a device following
     a setting. Untouched, the **stored** value goes back unchanged. */
  const [limitTouched, setLimitTouched] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  if (data && seededFrom !== data.id) {
    setSeededFrom(data.id);
    setName(data.name);
    setLimit(
      data.rate_limit_per_minute ?? data.effective_rate_limit_per_minute ?? '',
    );
    setLimitTouched(false);
    setRefusal(null);
  }

  const saveSettings = useMutation({
    mutationFn: renameDevice,
    onSuccess: (next) => {
      setRefusal(null);
      refresh(next);
      // Saving finishes the job, so the card goes away — the same exit
      // Cancel takes. Leaving it open made Save and Cancel look like they
      // did unrelated things: one of them closed the card and the other
      // appeared to do nothing, because the only visible effect was a
      // field already showing what you had typed.
      //
      // Only on a surface that *can* close. On the `/devices/:id` route
      // there is nothing to close and the card correctly stays, now
      // showing the saved values.
      onClose?.();
    },
    // The 409 is rendered as the server wrote it. Nothing here retries
    // with a suffix and nothing here rewords it: "you already have a
    // device called 'router'" is more useful than any sentence this
    // component could compose, and it is the sentence the API guarantees.
    onError: (error: Error) => setRefusal(refusalText(error)),
  });

  const trimmed = name.trim();
  /** Empty is *inherit*, which travels as `null`. `0` is *muted* and is a
   *  different thing, which is why this is not `|| null`. */
  const limitValue = limitTouched
    ? limit === ''
      ? null
      : limit
    : (data?.rate_limit_per_minute ?? null);
  const dirty =
    data !== undefined &&
    (trimmed !== data.name || limitValue !== data.rate_limit_per_minute);

  const submit = () => {
    if (!data || trimmed === '' || saveSettings.isPending || !dirty) return;
    saveSettings.mutate({
      id: data.id,
      name: trimmed,
      rate_limit_per_minute: limitValue,
    });
  };

  /** Cancel means *nothing you typed is kept*. On a modal that is closing
   *  it; on the route there is nothing to close, so it reverts instead —
   *  the same promise, honoured the only way that surface can. */
  const cancelEdits = () => {
    if (onClose) {
      onClose();
      return;
    }
    if (data) {
      setName(data.name);
      setLimit(
        data.rate_limit_per_minute ?? data.effective_rate_limit_per_minute ?? '',
      );
    }
    setLimitTouched(false);
    setRefusal(null);
  };


  const remove = useMutation({
    mutationFn: deleteDevice,
    onSuccess: () => {
      setConfirmDelete(false);
      setDeleteError(null);
      // No `refresh(next)`: that seeds the cache with the updated row, and
      // a deleted device has no row to seed. Drop both lists instead.
      void client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
      void client.invalidateQueries({ queryKey: BOARD_QUERY_KEY });
      onDeleted?.();
    },
    onError: (err: Error) => setDeleteError(err.message),
  });

  const rotate = useMutation({
    mutationFn: rotateDeviceSecret,
    onSuccess: (result) => {
      setIssued(result);
      setConfirmRotate(false);
      setRotateError(null);
      refresh(result.device);
    },
    onError: (err: Error) => setRotateError(refusalText(err)),
  });


  if (!canRead) {
    return (
      <Alert
        color="gray"
        variant="light"
        title="Not available to this account"
        data-testid="detail-refused"
      >
        <Text size="sm">
          Managing devices needs the <code>{DEVICE_PERMISSION}</code>{' '}
          permission. This is a refusal, not a missing device — ask an
          administrator for the permission rather than assuming this device
          does not exist.
        </Text>
      </Alert>
    );
  }

  if (isLoading) {
    return (
      <Text size="sm" data-testid="detail-loading">
        Loading…
      </Text>
    );
  }

  if (error) {
    return (
      <Alert
        color="gray"
        variant="light"
        /* 404 is *this device does not exist, or is not yours* — the
           server deliberately does not distinguish the two, because a
           403 would confirm the row exists. Said in those words
           rather than as a load failure, which would read as a bug. */
        title={
          error instanceof ApiError && error.status === 404
            ? 'No such device'
            : 'Could not load this device'
        }
        data-testid="detail-error"
      >
        <Text size="sm" ff="monospace">
          {refusalText(error)}
        </Text>
      </Alert>
    );
  }

  if (!data) return null;

  return (
    <Stack gap="lg" data-testid="device-detail">
      <Stack gap={4}>
        {/* One line, both labels above their own field. They were stacked
            — a full-width name over a rate limit whose label sat beside the
            box — so two short fields ate three rows and the two labels were
            in different places. `flex-end` keeps the boxes aligned when the
            labels wrap. */}
        <Group gap="md" align="flex-end" wrap="nowrap">
          <TextInput
            label="Name"
            value={name}
            disabled={saveSettings.isPending}
            onChange={(event) => setName(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') submit();
              if (event.key === 'Escape') cancelEdits();
            }}
            data-testid="device-name-input"
            style={{ flex: 1, minWidth: 240 }}
          />
          <NumberInput
            label="Rate limit"
            aria-label="Updates per minute"
            value={limit}
            min={0}
            disabled={saveSettings.isPending}
            placeholder="inherit"
            onChange={(next) => {
              setLimitTouched(true);
              setLimit(typeof next === 'number' ? next : '');
            }}
            data-testid="detail-limit-input"
            styles={{ input: { width: 110 } }}
            w={110}
          />
          <Text size="sm" c="dimmed" pb={7}>
            per minute
          </Text>
        </Group>
        {refusal ? (
          <Alert
            color="gray"
            variant="light"
            title="That did not work"
            data-testid="device-save-refusal"
          >
            <Text size="sm" ff="monospace">
              {refusal}
            </Text>
          </Alert>
        ) : null}
        <Group gap="md">
          {/* Prefixed: on a line that also carries "seen" and "created",
              a bare `ddns-…` string was the one item that did not say
              what it is — and it is the half of the credential that has
              to be typed into a router. */}
          <span className="ddns-cell" data-testid="detail-username">
            <strong>Username:</strong> {data.username}
          </span>
          <span
            className="ddns-cell"
            title={absoluteTitle(data.last_seen_at)}
            data-testid="detail-last-seen"
          >
            seen {formatAge(data.last_seen_at)}
          </span>
          <span
            className="ddns-cell"
            title={absoluteTitle(data.created_at)}
            data-testid="detail-created"
          >
            created {formatAge(data.created_at)}
          </span>
        </Group>
      </Stack>

      <MigratedNotice origin={data.credential_origin} />


      {/* A link to the list, not a second copy of it.
      
          The card used to draw this device's names itself — the same rows
          the board draws, rendered a second way, inside a modal you opened
          to change a rate limit. Two renderings of one population is the
          thing that goes out of step: the board grew filters, a `+` per
          row and a log link per row, and none of that reached the copy in
          here.
      
          So the card says how many and points at the real list, filtered
          to this device. The count still comes from `hostname_count` on the
          device itself rather than from the board payload, so it does not
          depend on a second request having arrived. */}
      <Group gap="sm" align="center">
        <span className="ddns-th">Names</span>
        <Anchor
          href={boardForDeviceHref(data.id)}
          size="sm"
          data-testid="detail-names-link"
        >
          {data.hostname_count === 0
            ? 'No names yet — add one on the board'
            : `Show ${data.hostname_count} name${
                data.hostname_count === 1 ? '' : 's'
              } on the board`}
        </Anchor>
      </Group>

      <Divider />

      <Stack gap="xs">
        {/* Rotate sits beside the heading. The sentence it used to
            carry — "the device stops working until it is reconfigured" —
            was a warning shown before anyone had asked for anything, on a
            screen you open to read a rate limit. It now lives in the
            confirmation, answering the question you have just asked. */}
        {/* The card's one row of verbs: destructive on the left, the
            rest on the right. They were in two places — Save and Cancel
            mid-modal above the username and the names list, Rotate at the
            bottom under a `Credential` heading — so the card appeared to
            end twice. The heading is gone with them: `Rotate Credentials`
            already says what it rotates, and a label whose only job is to
            introduce one button is a row of chrome. */}
        <Group justify="space-between" align="center">
          <Button
            size="xs"
            variant="default"
            disabled={rotate.isPending}
            onClick={() => setConfirmRotate(true)}
            data-testid="detail-rotate"
          >
            Rotate Credentials
          </Button>
          <Group gap="sm">
            {/* A filled red button, not a subtle one: it is the only
                action on this card that cannot be undone, and the muted
                treatment read as a link. It still asks before it acts —
                the confirmation is what actually protects the device, and
                the colour is what stops the click being casual. */}
            <Button
              size="xs"
              color="red"
              disabled={remove.isPending || saveSettings.isPending}
              onClick={() => setConfirmDelete(true)}
              data-testid="detail-delete"
            >
              Delete this device
            </Button>
            <Button
              size="xs"
              variant="default"
              disabled={saveSettings.isPending}
              onClick={cancelEdits}
              data-testid="device-cancel"
            >
              Cancel
            </Button>
            <Button
              size="xs"
              disabled={trimmed === '' || !dirty || saveSettings.isPending}
              onClick={submit}
              data-testid="device-save"
            >
              Save
            </Button>
          </Group>
        </Group>
        {/* The same modal creation uses, not an inline block. A rotated
            credential is the identical object shown for the identical
            reason, and printing it inline here put it inside the card it
            was rotated from — competing with the form and scrolling away
            like ordinary content. `zIndex` in `SecretOnceModal` is what
            lets it sit above this card, which is itself a modal. */}
        <SecretOnceModal
          issued={issued}
          onDismiss={() => setIssued(null)}
          title="Secret rotated"
        />
        {rotateError ? (
          <Alert
            color="gray"
            variant="light"
            title="That did not work"
            data-testid="detail-rotate-error"
          >
            <Text size="sm" ff="monospace">
              {rotateError}
            </Text>
          </Alert>
        ) : null}
      </Stack>

      <Modal
        opened={confirmRotate}
        onClose={() => setConfirmRotate(false)}
        title="Rotate this secret?"
      >
        <Stack gap="sm">
          {/* Said *before* the button. Rotation is a different
              operation from fixing a typo and it is why it is not
              on the same Save button as the name. */}
          <Text size="sm" data-testid="detail-rotate-warning">
            Rotating issues a new secret and stops the old one working
            immediately. Any router still configured with the old secret
            will start failing to update until you reconfigure it. The new
            secret is shown once and cannot be recovered afterwards.
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={rotate.isPending}
              onClick={() => rotate.mutate(data.id)}
              data-testid="detail-rotate-confirm"
            >
              Rotate
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title="Delete this device?"
        zIndex={400}
        data-testid="detail-delete-confirm"
      >
        <DdnsPortalScope>
          <Stack gap="sm">
            <Text size="sm">
              <strong>{data.name}</strong> is deleted, along with its DDNS
              credential. Any name it updates is left with no device, so
              nothing will update it until you assign another. This cannot
              be undone.
            </Text>
            {deleteError ? (
              <Alert color="gray" variant="light" data-testid="detail-delete-error">
                <Text size="sm" ff="monospace">
                  {deleteError}
                </Text>
              </Alert>
            ) : null}
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="default"
                disabled={remove.isPending}
                onClick={() => setConfirmDelete(false)}
                data-testid="detail-delete-cancel"
              >
                Cancel
              </Button>
              <Button
                size="xs"
                color="red"
                disabled={remove.isPending}
                onClick={() => remove.mutate(data.id)}
                data-testid="detail-delete-confirmed"
              >
                Delete {data.name}
              </Button>
            </Group>
          </Stack>
        </DdnsPortalScope>
      </Modal>
    </Stack>
  );
}

/** The modal entrance — §17's *"click a row → a modal opens"*.
 *
 * `size` is `cards.ts`'s derived width, not a Mantine keyword. This is
 * the card that actually carries the signature element — a device's
 * names render as resolution strips inside it — so §17's condition
 * lands here first: *"a modal that wraps the signature element
 * reintroduces exactly the failure §12 was written to avoid"*. Mantine's
 * `lg` is 620px, which is the drawer §12 rejected. The rendered body is
 * measured in a browser by `tests-e2e/card-affordance.spec.ts` rather
 * than left to this arithmetic.
 */
export function DeviceCardModal({
  deviceId,
  onClose,
}: {
  deviceId: number | null;
  onClose: () => void;
}) {
  return (
    <Modal
      opened={deviceId !== null}
      onClose={onClose}
      title="Device"
      size={CARD_MODAL_WIDTH_PX}
      padding={CARD_MODAL_PADDING_PX}
      {...CARD_MODAL_PROPS}
      styles={CARD_MODAL_STYLES}
      data-testid="device-card-modal"
    >
      {/* Portalled to `document.body`, outside `data-ddns-root`. Without
          this the strips below render with no rail, no data face and
          none of the palette — see `DdnsPortalScope`, which was written
          after a browser measured it. */}
      <DdnsPortalScope>
        {deviceId === null ? null : <DeviceCard
            deviceId={deviceId}
            onClose={onClose}
            onDeleted={onClose}
          />}
      </DdnsPortalScope>
    </Modal>
  );
}
