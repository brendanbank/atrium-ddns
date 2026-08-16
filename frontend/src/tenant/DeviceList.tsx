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
  Alert,
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
  type Device,
  type DeviceSecret,
} from '../api/devices';
import { absoluteTitle, formatAge } from '../board/format';
import { MigratedNotice, SecretOnce } from './SecretOnce';

function DeviceLine({
  device,
  onRotate,
  onDelete,
  busy,
}: {
  device: Device;
  onRotate: (device: Device) => void;
  onDelete: (device: Device) => void;
  busy: boolean;
}) {
  return (
    <Stack gap="xs" data-testid={`device-${device.name}`}>
      <div className="ddns-device__line" style={{ cursor: 'default' }}>
        <span />
        <span className="ddns-data">{device.name}</span>
        <span
          className="ddns-station__time"
          title={absoluteTitle(device.last_seen_at)}
        >
          {formatAge(device.last_seen_at)}
        </span>
        <span className="ddns-station__time">{device.username}</span>
      </div>
      <MigratedNotice origin={device.credential_origin} />
      <Group gap="xs">
        <Button
          size="xs"
          variant="default"
          disabled={busy}
          onClick={() => onRotate(device)}
          data-testid={`rotate-${device.name}`}
        >
          Rotate secret
        </Button>
        <Button
          size="xs"
          variant="default"
          disabled={busy}
          onClick={() => onDelete(device)}
          data-testid={`delete-${device.name}`}
        >
          Delete
        </Button>
        <Text size="xs" c="dimmed" data-testid={`names-${device.name}`}>
          {device.hostname_count} hostname
          {device.hostname_count === 1 ? '' : 's'}
        </Text>
      </Group>
    </Stack>
  );
}

export function DeviceList({ devices }: { devices: Device[] }) {
  const client = useQueryClient();
  // The secret lives here and nowhere else. Not in the query cache, not
  // in storage, not in a ref that survives a remount.
  const [issued, setIssued] = useState<DeviceSecret | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [limit, setLimit] = useState<number | ''>('');
  const [confirmRotate, setConfirmRotate] = useState<Device | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () =>
    client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });

  const create = useMutation({
    mutationFn: createDevice,
    onSuccess: (result) => {
      setIssued(result);
      setCreating(false);
      setName('');
      setLimit('');
      setError(null);
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

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
      void invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  const busy = create.isPending || rotate.isPending || remove.isPending;

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

      {issued ? (
        <SecretOnce issued={issued} onDismiss={() => setIssued(null)} />
      ) : null}

      <Group justify="space-between">
        <div className="ddns-board__head">
          <span />
          <span className="ddns-label">device</span>
          <span className="ddns-label">last seen</span>
          <span className="ddns-label">username</span>
        </div>
      </Group>

      {devices.length === 0 ? (
        <Text size="sm" data-testid="devices-empty">
          {/* §4.5's voice: the next action, in the body face. */}
          You have no devices yet. Add one to get a DDNS username and password.
        </Text>
      ) : (
        devices.map((device) => (
          <DeviceLine
            key={device.id}
            device={device}
            busy={busy}
            onRotate={setConfirmRotate}
            onDelete={(target) => remove.mutate(target.id)}
          />
        ))
      )}

      <Group>
        <Button
          size="xs"
          onClick={() => setCreating(true)}
          data-testid="add-device"
        >
          Add a device
        </Button>
      </Group>

      <Modal
        opened={creating}
        onClose={() => setCreating(false)}
        title="Add a device"
      >
        <Stack gap="sm">
          <TextInput
            label="Name"
            description="What you call it. The username is generated for you."
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            data-testid="device-name"
          />
          <NumberInput
            label="Rate limit (per minute)"
            description="Leave empty to inherit the installation default. Zero means this device may never call — which is not the same thing."
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
