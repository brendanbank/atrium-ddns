/** The devices surface — create, rotate, delete.
 *
 * A ledger in the board's shape (`ui-design.md` §3.6) rather than a
 * table with a sort control, so the two device surfaces read as one
 * product. The board answers *which device stopped talking*; this one
 * answers *what credentials does this device have and how do I replace
 * them*, and they share a vocabulary rather than each inventing one.
 *
 * The secret handling is all in `SecretOnce.tsx`. What is here is the
 * part that surrounds it: the issued secret lives in component state,
 * is rendered by exactly one element, and is dropped on dismissal and
 * on any navigation. Nothing writes it anywhere else.
 *
 * `; last seen` renders `never` for a null, never an epoch-derived age
 * — `ui-design.md` §4.2's second prohibition, and the reason it is a
 * prohibition rather than a preference is that `now - 0` is fifty-six
 * years and makes every freshness rule fire for a full cadence after
 * each deploy.
 */
import { useState } from 'react';
import {
  Tooltip,
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Group,
  Modal,
  NumberInput,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  DEVICES_QUERY_KEY,
  createDevice,
  deleteDevice,
  rotateDeviceSecret,
  updateDeviceLimit,
  type Device,
  type DeviceSecret,
} from '../api/devices';
import { absoluteTitle, formatAge, rateLimitSummary } from '../board/format';
import { opensInThisTab } from '../cards';
import { deviceHrefParam } from '../paths';
import {
  IconGauge,
  IconRefresh,
  IconTrash,
} from '@tabler/icons-react';

import { DdnsPortalScope } from '../host/DdnsRoot';
import { MigratedNotice, SecretOnce } from './SecretOnce';

function DeviceLine({
  device,
  index,
  onOpen,
  onRotate,
  onDelete,
  onEditLimit,
  busy,
}: {
  device: Device;
  /** Stripe parity, stated rather than counted — the head is a sibling
   *  in the same grid, so `:nth-child(even)` is off by one, and the
   *  selector that would be correct is rejected by lightningcss. */
  index: number;
  onOpen: (id: number) => void;
  onRotate: (device: Device) => void;
  onDelete: (device: Device) => void;
  onEditLimit: (device: Device) => void;
  busy: boolean;
}) {
  return (
    <div
      className="ddns-devices__row"
      data-stripe={index % 2 === 1 ? 'on' : 'off'}
      data-testid={`device-${device.name}`}
    >
      {/* #89 made the name the way in to `/atrium-ddns/devices/:id`, and
          #97 gave it an affordance that is not colour — `.ddns-data`
          sets `--ddns-ink`, which cancels Mantine's link colour, so the
          underline in `ddns.css` §2a is what says it is clickable.

          Still an anchor with a real `href`: copy-link, middle-click and
          cmd/ctrl-click navigate. Only the plain left click is
          intercepted, and it opens the card. */}
      <Anchor
        /* The `?device=` form, not `/devices/:id`. A plain click is
           intercepted and opens the modal; the href is what a
           cmd-click or "copy link address" hands you, and those two
           pointing at different addresses is how you end up with a link
           that behaves differently from the thing you clicked. The
           path route still resolves for old links — it renders the same
           card as a full page. */
        href={deviceHrefParam(device.id)}
        className="ddns-data"
        onClick={(event) => {
          if (!opensInThisTab(event.nativeEvent)) return;
          event.preventDefault();
          onOpen(device.id);
        }}
        data-testid={`open-${device.name}`}
      >
        {device.name}
      </Anchor>
      <span className="ddns-cell" title={absoluteTitle(device.last_seen_at)}>
        {formatAge(device.last_seen_at)}
      </span>
      <span className="ddns-cell">{device.username}</span>
      <span className="ddns-cell" data-testid={`names-${device.name}`}>
        {device.hostname_count} name{device.hostname_count === 1 ? '' : 's'}
      </span>
      {/* #73's AC 4: the stored limit is *displayed*, not merely accepted
          at creation. Unconditionally — a limit shown only when it is
          unusual is a limit nobody can check. */}
      <span className="ddns-cell" data-testid={`limit-summary-${device.name}`}>
        {rateLimitSummary(device)}
      </span>
      {/* Icons, not three labelled buttons on a line of their own. Each
          keeps a `Tooltip` and an `aria-label`: an icon-only control
          without a name is a control only the person who built it can
          use, and the two destructive ones here are exactly where that
          matters. */}
      <span className="ddns-devices__actions">
        <Tooltip label="Rotate secret" withArrow>
          <ActionIcon
            variant="subtle"
            color="gray"
            disabled={busy}
            aria-label={`Rotate the secret for ${device.name}`}
            onClick={() => onRotate(device)}
            data-testid={`rotate-${device.name}`}
          >
            <IconRefresh size={16} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Rate limit" withArrow>
          <ActionIcon
            variant="subtle"
            color="gray"
            disabled={busy}
            aria-label={`Change the rate limit for ${device.name}`}
            onClick={() => onEditLimit(device)}
            data-testid={`limit-${device.name}`}
          >
            <IconGauge size={16} />
          </ActionIcon>
        </Tooltip>
        <Tooltip label="Delete" withArrow>
          <ActionIcon
            variant="subtle"
            color="red"
            disabled={busy}
            aria-label={`Delete ${device.name}`}
            onClick={() => onDelete(device)}
            data-testid={`delete-${device.name}`}
          >
            <IconTrash size={16} />
          </ActionIcon>
        </Tooltip>
      </span>
      <MigratedNotice origin={device.credential_origin} />
    </div>
  );
}

export function DeviceList({
  devices,
  onOpen,
}: {
  devices: Device[];
  /** Navigates. The list does not know that opening a device is a URL
   *  change — `DevicesPage` owns that, so there is one place that
   *  decides what a device address is, and the modal survives a reload
   *  because the address carries it. */
  onOpen: (id: number) => void;
}) {
  const client = useQueryClient();
  // The secret lives here and nowhere else. Not in the query cache, not
  // in storage, not in a ref that survives a remount.
  const [issued, setIssued] = useState<DeviceSecret | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [limit, setLimit] = useState<number | ''>('');
  const [confirmRotate, setConfirmRotate] = useState<Device | null>(null);
  /** Delete used to fire straight from the row — `onDelete={(target) =>
   *  remove.mutate(target.id)}`, no dialog, no undo. One misplaced click
   *  destroyed a device and every hostname assignment pointing at it,
   *  and the icon that now triggers it is a 16px target beside two
   *  others. Rotate already asked; delete does the same. */
  const [confirmDelete, setConfirmDelete] = useState<Device | null>(null);
  const [editingLimit, setEditingLimit] = useState<Device | null>(null);
  const [nextLimit, setNextLimit] = useState<number | ''>('');
  const [error, setError] = useState<string | null>(null);
  /** Which device's card is open, if any. `null` is *closed*, never
   *  *device zero*. */

  const invalidate = () =>
    client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });

  const create = useMutation({
    mutationFn: createDevice,
    onSuccess: (result) => {
      // **The create modal stays open.** The secret opens over it, and
      // dismissing the secret closes both — see `dismissSecret`.
      //
      // Closing the form the instant the secret appeared put the one
      // string that can never be recovered on a page that had just
      // moved under the pointer. A modal over a modal is the shape that
      // says "you are not finished yet", and the form behind it is the
      // context for what you are being handed.
      setIssued(result);
      setError(null);
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  /** One exit for the secret, whichever mutation produced it.
   *
   *  Creation leaves the form open behind; rotation has no form behind,
   *  and `setCreating(false)` is a no-op there. Writing it once rather
   *  than per call site is what keeps the two paths from drifting into
   *  "rotate leaves the create modal open". */
  const dismissSecret = () => {
    setIssued(null);
    setCreating(false);
    setName('');
    setLimit('');
  };

  const rotate = useMutation({
    mutationFn: rotateDeviceSecret,
    onSuccess: (result) => {
      setIssued(result);
      setConfirmRotate(null);
      setError(null);
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: deleteDevice,
    onSuccess: () => {
      setError(null);
      setConfirmDelete(null);
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  // #73. Deliberately its own mutation and not a branch of `create`:
  // the whole reason this route exists is that changing a limit must
  // not go anywhere near the credential, and sharing a code path with
  // the call that mints one is how that stops being true later.
  const relimit = useMutation({
    mutationFn: updateDeviceLimit,
    onSuccess: () => {
      setEditingLimit(null);
      setNextLimit('');
      setError(null);
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  const busy =
    create.isPending ||
    rotate.isPending ||
    remove.isPending ||
    relimit.isPending;

  const openLimit = (device: Device) => {
    setEditingLimit(device);
    // Seeded from the **stored** value, not the effective one: opening
    // the box on an inheriting device and pressing Save must not turn
    // an inherited 30 into a per-device 30 that stops following the
    // installation default.
    setNextLimit(
      device.rate_limit_per_minute === null ? '' : device.rate_limit_per_minute,
    );
  };

  return (
    <Stack gap="md">
      {error ? (
        <Alert color="gray" variant="light" title="That did not work" data-testid="device-error">
          {/* Diagnostics in full — the status and the server's own
              words. Redact secrets, never diagnostics. */}
          <Text size="sm" ff="monospace">
            {error}
          </Text>
        </Alert>
      ) : null}

      {devices.length === 0 ? (
        <Text size="sm" data-testid="devices-empty">
          {/* §4.5's voice: the next action, in the body face. */}
          You have no devices yet. Add one to get a DDNS username and password.
        </Text>
      ) : (
        <div className="ddns-devices" data-testid="devices-table">
          {/* Sentence-case headings on the shared `.ddns-th`, and no `; `
              marker: `.ddns-label` injects one via `::before`, and that
              borrowing belongs to the strip's station labels rather than
              to a column heading a newcomer meets first. */}
          <div className="ddns-devices__head">
            <span className="ddns-th">Device</span>
            <span className="ddns-th">Last seen</span>
            <span className="ddns-th">Username</span>
            <span className="ddns-th">Names</span>
            <span className="ddns-th">Rate limit</span>
            <span className="ddns-th" />
            <span className="ddns-th" />
          </div>
          {devices.map((device, index) => (
            <DeviceLine
              key={device.id}
              device={device}
              index={index}
              busy={busy}
                onOpen={onOpen}
                onRotate={setConfirmRotate}
                onEditLimit={openLimit}
                onDelete={setConfirmDelete}
              />
            ))}
          </div>
        )}

      <Modal
        opened={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete this device?"
        data-testid="device-delete-modal"
      >
        <DdnsPortalScope>
          {confirmDelete ? (
            <Stack gap="sm">
              <Text size="sm" data-testid="device-delete-warning">
                This deletes{' '}
                <span className="ddns-data">{confirmDelete.name}</span> and its
                credential. The {confirmDelete.hostname_count} name
                {confirmDelete.hostname_count === 1 ? '' : 's'} it updates{' '}
                <strong>survive</strong> — they are unassigned, not removed, and
                stop being updated until another device takes them over. The
                records already published stay in the zone with nothing
                maintaining them.
              </Text>
              <Group justify="flex-end">
                <Button
                  size="xs"
                  variant="default"
                  disabled={busy}
                  onClick={() => setConfirmDelete(null)}
                  data-testid="device-delete-cancel"
                >
                  Keep it
                </Button>
                <Button
                  size="xs"
                  color="red"
                  disabled={busy}
                  onClick={() => remove.mutate(confirmDelete.id)}
                  data-testid="device-delete-confirmed"
                >
                  Delete {confirmDelete.name}
                </Button>
              </Group>
            </Stack>
          ) : null}
        </DdnsPortalScope>
      </Modal>


      <Group>
        <Button
          size="xs"
          onClick={() => setCreating(true)}
          data-testid="add-device"
        >
          Add a device
        </Button>
      </Group>

      {/* Over the create modal, not instead of it. `zIndex` is explicit
          because Mantine gives every modal the same one by default, and
          two at the same level stack by mount order — which is the sort
          of thing that works until a re-render changes the order and the
          secret ends up behind the form that produced it. */}
      <Modal
        opened={issued !== null}
        onClose={dismissSecret}
        title="Device created"
        zIndex={400}
        data-testid="device-secret-modal"
      >
        {/* Portalled to `document.body`, outside `[data-ddns-root]`, so
            without this the secret renders with none of `ddns.css` — and
            `.ddns-data` is what makes the secret selectable as one
            monospaced run rather than reflowing prose. */}
        <DdnsPortalScope>
          {issued ? (
            <SecretOnce issued={issued} onDismiss={dismissSecret} />
          ) : null}
        </DdnsPortalScope>
      </Modal>

      <Modal
        opened={creating}
        onClose={() => setCreating(false)}
        title="Add a device"
      >
        <Stack gap="sm">
          <TextInput
            label="Name"
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            data-testid="device-name"
          />
          <NumberInput
            label="Rate limit (per minute)"
            value={limit}
            min={0}
            onChange={(value) =>
              setLimit(typeof value === 'number' ? value : '')
            }
            data-testid="device-limit"
          />
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={name.trim() === '' || busy}
              onClick={() =>
                create.mutate({
                  name: name.trim(),
                  // `null`, not `0`. An empty box means *inherit*, and
                  // coercing it to zero would mute the device.
                  rate_limit_per_minute: limit === '' ? null : limit,
                })
              }
              data-testid="device-submit"
            >
              Create
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={editingLimit !== null}
        onClose={() => setEditingLimit(null)}
        title={
          editingLimit ? `Rate limit for ${editingLimit.name}` : 'Rate limit'
        }
      >
        <Stack gap="sm">
          {/* Said before the button, like the rotate warning next to it
              — and saying the opposite thing, which is the point of the
              route: this one does *not* break the device. */}
          <Text size="sm" data-testid="limit-explainer">
            This changes how many updates the device may make per minute. It
            does not touch its username or its secret, so the router keeps
            working — that is why this exists rather than deleting and
            recreating the device, which issues a new credential and breaks it
            until someone reconfigures it.
          </Text>
          <NumberInput
            label="Rate limit (per minute)"
            value={nextLimit}
            min={0}
            disabled={busy}
            onChange={(value) =>
              setNextLimit(typeof value === 'number' ? value : '')
            }
            data-testid="limit-input"
          />
          {editingLimit ? (
            <Text size="xs" c="dimmed" data-testid="limit-current">
              Currently {rateLimitSummary(editingLimit)}.
            </Text>
          ) : null}
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={busy || editingLimit === null}
              onClick={() =>
                editingLimit
                  ? relimit.mutate({
                      id: editingLimit.id,
                      // `null`, not `0`. An empty box means *inherit*;
                      // coercing it to zero would mute the device.
                      rate_limit_per_minute:
                        nextLimit === '' ? null : nextLimit,
                    })
                  : undefined
              }
              data-testid="limit-submit"
            >
              Save
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={confirmRotate !== null}
        onClose={() => setConfirmRotate(null)}
        title="Rotate this secret?"
      >
        <Stack gap="sm">
          {/* Said *before* the button is pressed. A router still
              configured with the old secret starts answering `badauth`
              the moment this commits, and telling someone afterwards is
              telling them during the outage. */}
          <Text size="sm" data-testid="rotate-warning">
            Rotating issues a new secret and stops the old one working
            immediately. Any router still configured with the old secret will
            start failing to update until you reconfigure it. The new secret is
            shown once and cannot be recovered afterwards.
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={busy}
              onClick={() =>
                confirmRotate ? rotate.mutate(confirmRotate.id) : undefined
              }
              data-testid="rotate-confirm"
            >
              Rotate
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
