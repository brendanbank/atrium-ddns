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

import { boardQuery } from '../api/board';
import {
  DEVICES_QUERY_KEY,
  DEVICE_PERMISSION,
  deviceQuery,
  renameDevice,
  rotateDeviceSecret,
  updateDeviceLimit,
  type Device,
  type DeviceSecret,
} from '../api/devices';
import { ApiError } from '../api/http';
import { HostnameBlock } from '../board/DeviceBoard';
import { absoluteTitle, formatAge, rateLimitSummary } from '../board/format';
import { CARD_MODAL_PADDING_PX, CARD_MODAL_WIDTH_PX, CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { MigratedNotice, SecretOnce } from './SecretOnce';

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

/** The name, edited where it is displayed. */
function DeviceName({
  device,
  onSaved,
}: {
  device: Device;
  onSaved: (next: Device) => void;
}) {
  const [draft, setDraft] = useState(device.name);
  const [refusal, setRefusal] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: renameDevice,
    onSuccess: (next) => {
      setRefusal(null);
      onSaved(next);
    },
    // The 409 is rendered as the server wrote it. Nothing here retries
    // with a suffix and nothing here rewords it: "you already have a
    // device called 'router'" is more useful than any sentence this
    // component could compose, and it is the sentence the API contract
    // guarantees.
    onError: (error: Error) => setRefusal(refusalText(error)),
  });


  const submit = () => {
    const name = draft.trim();
    if (name === '' || rename.isPending) return;
    rename.mutate({
      id: device.id,
      name,
      // The **stored** limit, re-sent unchanged. See `renameDevice`:
      // the key is required, and sending the effective value would pin
      // an inheriting device to today's default.
      rate_limit_per_minute: device.rate_limit_per_minute,
    });
  };

  /* Always an input with Save beside it. No read mode, no Rename link.

     The toggle existed to keep the name looking like a heading until you
     asked to change it. The cost was a click before the most common edit
     on the page, and two states that had to agree about what the name
     currently is. A field that is always a field cannot disagree with
     itself, and `Save` stays disabled until the draft differs from the
     stored name — so the affordance still says whether there is anything
     to do. */
  return (
    <Stack gap="xs">
      <Group gap="sm" align="flex-end">
        <TextInput
          label="Name"
          value={draft}
          disabled={rename.isPending}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') submit();
            // Escape restores the stored name. There is nothing to close.
            if (event.key === 'Escape') setDraft(device.name);
          }}
          data-testid="device-name-input"
          style={{ minWidth: 280 }}
        />
        <Button
          size="xs"
          disabled={
            draft.trim() === '' ||
            draft.trim() === device.name ||
            rename.isPending
          }
          onClick={submit}
          data-testid="device-name-save"
        >
          Save
        </Button>
      </Group>
      {refusal ? (
        <Alert
          color="gray"
          variant="light"
          title="That name is taken"
          data-testid="device-name-refusal"
        >
          <Text size="sm" ff="monospace">
            {refusal}
          </Text>
        </Alert>
      ) : null}
    </Stack>
  );
}

/** The rate limit, with *inherit* as a choice rather than as an
 *  omission. */
function RateLimit({
  device,
  onSaved,
}: {
  device: Device;
  onSaved: (next: Device) => void;
}) {
  /* One field, prefilled with the limit actually in force — the
     per-device value if there is one, otherwise the installation default
     the server reports. So the box always opens showing the number the
     device is really being held to, which is what you came to read.

     **Clearing the box is how you go back to inheriting.** The checkbox
     that used to say so is gone at the operator's request, so empty is
     the only way `null` still reaches the API — and `null` is a value on
     this column, not an omission (#73). Without that mapping a device,
     once opened, could never inherit again. */
  const [value, setValue] = useState<number | ''>(
    device.rate_limit_per_minute ?? device.effective_rate_limit_per_minute ?? '',
  );
  const [refusal, setRefusal] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: updateDeviceLimit,
    onSuccess: (next) => {
      setRefusal(null);
      onSaved(next);
    },
    onError: (error: Error) => setRefusal(refusalText(error)),
  });

  /* One line: heading, field, checkbox, Save.
     The field is **prefilled with the effective limit** rather than left
     blank when the device inherits. A blank box next to the words
     "inherit the installation default (30)" made the operator read the
     number off the label and type it back in to change it by one.

     `inheriting` still travels as `null` on the wire — #73's rule that
     `null` is a value on this column and not an omission. Prefilling is
     a display choice; it does not turn an inheriting device into a
     pinned one unless the box is ticked off. */
  return (
    <Stack gap="xs">
      <Group gap="md" align="center" wrap="nowrap">
        <span className="ddns-th">Rate limit</span>
        <NumberInput
          aria-label="Updates per minute"
          value={value}
          min={0}
          disabled={save.isPending}
          placeholder="inherit"
          onChange={(next) => setValue(typeof next === 'number' ? next : '')}
          data-testid="detail-limit-input"
          styles={{ input: { width: 110 } }}
          w={110}
        />
        <Text size="sm" c="dimmed">
          per minute
        </Text>
        <Button
          size="xs"
          disabled={save.isPending}
          onClick={() =>
            save.mutate({
              id: device.id,
              // `null` is the *inherit* choice, made explicitly. `0`
              // would mute the device, which is a different thing.
              // Empty means inherit. `0` would mute the device, which is
              // a different thing and is why this is not `|| 0`.
              rate_limit_per_minute: value === '' ? null : value,
            })
          }
          data-testid="detail-limit-save"
        >
          Save
        </Button>
      </Group>
      <Text size="xs" c="dimmed" data-testid="detail-limit-current">
        Currently {rateLimitSummary(device)}. Over this,{' '}
        <code>/nic/update</code> answers <code>abuse</code> and publishes
        nothing.
      </Text>
      {refusal ? (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="detail-limit-refusal"
        >
          <Text size="sm" ff="monospace">
            {refusal}
          </Text>
        </Alert>
      ) : null}
    </Stack>
  );
}

export interface DeviceCardProps {
  /** The device this card is about. Parsed from the pathname by the
   *  route, taken from the row by either modal. */
  deviceId: number;
}

export function DeviceCard({ deviceId }: DeviceCardProps) {
  const client = useQueryClient();
  const hasPerm = usePerm();
  const canRead = hasPerm(DEVICE_PERMISSION);

  const { data, isLoading, error } = useQuery(
    deviceQuery(deviceId, { enabled: canRead }),
  );
  // The strips arrive from the board, computed. Every verdict on them —
  // the five `DnsCheckStatus` values, both joint verdicts, the collapse
  // denominator — is decided server-side, and fetching them from a
  // second endpoint that rebuilt them would be the two-implementations
  // defect `api/board.ts` opens by forbidding.
  const board = useQuery(boardQuery({ enabled: canRead }));

  // The secret lives here and nowhere else — not in the query cache,
  // not in storage, not in a ref that survives a remount.
  const [issued, setIssued] = useState<DeviceSecret | null>(null);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [rotateError, setRotateError] = useState<string | null>(null);

  const refresh = (next: Device) => {
    client.setQueryData([...DEVICES_QUERY_KEY, next.id], next);
    // The list and the board both show this device, and both are now
    // one rename behind.
    void client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
  };

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

  const hostnames =
    board.data?.devices.find((entry) => entry.id === deviceId)?.hostnames ?? [];

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
        <DeviceName device={data} onSaved={refresh} />
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

      <RateLimit device={data} onSaved={refresh} />

      <Stack gap="xs">
        <span className="ddns-th">Names this device updates</span>
        {board.isLoading ? (
          <Text size="sm" data-testid="detail-strips-loading">
            Loading…
          </Text>
        ) : hostnames.length === 0 ? (
          <Text size="sm" data-testid="detail-no-names">
            {/* The count on the device row and the strips below it
                are two readings of one population, so they are
                reported together rather than letting an empty list
                imply a zero the other reading contradicts. */}
            {data.hostname_count === 0
              ? 'This device has no names. Assign one to start tracking it.'
              : `This device has ${data.hostname_count} name${
                  data.hostname_count === 1 ? '' : 's'
                }, and the board has not loaded them yet.`}
          </Text>
        ) : (
          hostnames.map((hostname) => (
            <HostnameBlock key={hostname.id} hostname={hostname} />
          ))
        )}
      </Stack>

      <Divider />

      <Stack gap="xs">
        {/* Rotate sits beside the heading. The sentence it used to
            carry — "the device stops working until it is reconfigured" —
            was a warning shown before anyone had asked for anything, on a
            screen you open to read a rate limit. It now lives in the
            confirmation, answering the question you have just asked. */}
        <Group gap="sm" align="center">
          <span className="ddns-th">Credential</span>
          <Button
            size="xs"
            variant="default"
            disabled={rotate.isPending}
            onClick={() => setConfirmRotate(true)}
            data-testid="detail-rotate"
          >
            Rotate
          </Button>
        </Group>
        {issued ? (
          <SecretOnce issued={issued} onDismiss={() => setIssued(null)} />
        ) : null}
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
        {deviceId === null ? null : <DeviceCard deviceId={deviceId} />}
      </DdnsPortalScope>
    </Modal>
  );
}
