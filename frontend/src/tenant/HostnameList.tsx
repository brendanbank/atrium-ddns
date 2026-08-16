/** Names — create, assign, reassign, delete.
 *
 * The surface that closes the loop. #44 built the board, #45 built zones
 * and devices, #46 built the log, and none of them could create the
 * object the other three describe.
 *
 * ## Three states for the device column, not two
 *
 * `Not assigned yet` is a real configuration state and is rendered as a
 * choice rather than as a blank: the model allows a name to exist before
 * anything is assigned to it, and `ON DELETE SET NULL` means a name
 * outlives the router that was updating it. A blank cell would read as
 * missing data, which is the `n/a` is never `0` failure wearing a
 * select's clothing.
 *
 * ## No validation here
 *
 * There is no regex in this file and there must never be one. The server
 * decides validity with the same two functions `/nic/update` uses, and
 * its refusal is rendered verbatim — see `api/hostnames.ts` for why a
 * third opinion would be worse than a round trip.
 *
 * What the form *does* do is show the exact string it is about to send,
 * because it trims and the API does not. `will send:` is not decoration:
 * without it, a pasted trailing space produces a refusal about a value
 * that does not look like what is on screen.
 */
import { useState } from 'react';
import {
  Alert,
  Button,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import type { Device } from '../api/devices';
import type { Domain } from '../api/domains';
import {
  HOSTNAMES_QUERY_KEY,
  assignDevice,
  createHostname,
  deleteHostname,
  type Hostname,
} from '../api/hostnames';
import { absoluteTitle, formatAge } from '../board/format';

/** The `value` a Mantine `Select` uses for *no device*. `Select` speaks
 *  `string | null`, and `null` is already how it spells "nothing
 *  chosen" — which is a different fact from "chosen: unassigned". Two
 *  facts, two values. */
const UNASSIGNED = 'unassigned';

function deviceOptions(devices: Device[]) {
  return [
    { value: UNASSIGNED, label: 'Not assigned yet' },
    ...devices.map((device) => ({
      value: String(device.id),
      label: device.name,
    })),
  ];
}

function HostnameLine({
  hostname,
  devices,
  onAssign,
  onDelete,
  busy,
}: {
  hostname: Hostname;
  devices: Device[];
  onAssign: (hostname: Hostname, deviceId: number | null) => void;
  onDelete: (hostname: Hostname) => void;
  busy: boolean;
}) {
  return (
    <Stack gap="xs" data-testid={`hostname-${hostname.name}`}>
      <div className="ddns-device__line" style={{ cursor: 'default' }}>
        <span />
        <span className="ddns-data">{hostname.name}</span>
        <span
          className="ddns-station__time"
          title={absoluteTitle(hostname.last_updated_at)}
        >
          {/* `never` for a null, never an epoch-derived age. Nothing has
              been published until a `good` aggregate lands. */}
          {formatAge(hostname.last_updated_at)}
        </span>
        <span className="ddns-station__time">{hostname.domain_name}</span>
      </div>
      <Group gap="xs">
        <Select
          size="xs"
          aria-label={`Device for ${hostname.name}`}
          data={deviceOptions(devices)}
          value={
            hostname.device_id === null ? UNASSIGNED : String(hostname.device_id)
          }
          disabled={busy}
          allowDeselect={false}
          onChange={(value) =>
            onAssign(
              hostname,
              value === null || value === UNASSIGNED ? null : Number(value),
            )
          }
          data-testid={`assign-${hostname.name}`}
        />
        <Button
          size="xs"
          variant="default"
          disabled={busy}
          onClick={() => onDelete(hostname)}
          data-testid={`delete-hostname-${hostname.name}`}
        >
          Delete
        </Button>
      </Group>
    </Stack>
  );
}

export function HostnameList({
  hostnames,
  domains,
  devices,
}: {
  hostnames: Hostname[];
  domains: Domain[];
  devices: Device[];
}) {
  const client = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [zone, setZone] = useState<string | null>(null);
  const [device, setDevice] = useState<string>(UNASSIGNED);
  const [confirmDelete, setConfirmDelete] = useState<Hostname | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () =>
    client.invalidateQueries({ queryKey: HOSTNAMES_QUERY_KEY });
  const fail = (err: Error) => setError(err.message);
  const done = () => {
    setError(null);
    setCreating(false);
    setConfirmDelete(null);
    setName('');
    setDevice(UNASSIGNED);
    void invalidate();
  };

  const create = useMutation({
    mutationFn: createHostname,
    onSuccess: done,
    onError: fail,
  });
  const assign = useMutation({
    mutationFn: (input: { id: number; deviceId: number | null }) =>
      assignDevice(input.id, input.deviceId),
    onSuccess: done,
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: deleteHostname,
    onSuccess: done,
    onError: fail,
  });

  const busy = create.isPending || assign.isPending || remove.isPending;

  const selectedZone = domains.find((d) => String(d.id) === zone) ?? null;
  // Trimmed, because the form trims and the API does not. Shown to the
  // user rather than applied behind their back.
  const willSend = name.trim();

  return (
    <Stack gap="md">
      {error ? (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="hostname-error"
        >
          {/* Diagnostics in full — the server's own words, including its
              explanation of *why* a name was refused and which wire
              status the same name would have produced. */}
          <Text size="sm" ff="monospace">
            {error}
          </Text>
        </Alert>
      ) : null}

      <div className="ddns-board__head">
        <span />
        <span className="ddns-label">name</span>
        <span className="ddns-label">last published</span>
        <span className="ddns-label">zone</span>
      </div>

      {domains.length === 0 ? (
        <Text size="sm" data-testid="hostnames-no-zone">
          {/* The next action, not a dead end. A name has to live in a
              zone, so there is nothing useful to offer until one
              exists. */}
          You have no zones yet, and a name has to live in one. Add a zone
          first, then come back and register a name under it.
        </Text>
      ) : hostnames.length === 0 ? (
        <Text size="sm" data-testid="hostnames-empty">
          You have no names yet. Register one, then point a device at it — the
          board draws a resolution strip once that device has published an
          address.
        </Text>
      ) : (
        hostnames.map((hostname) => (
          <HostnameLine
            key={hostname.id}
            hostname={hostname}
            devices={devices}
            busy={busy}
            onAssign={(target, deviceId) =>
              assign.mutate({ id: target.id, deviceId })
            }
            onDelete={setConfirmDelete}
          />
        ))
      )}

      <Group>
        <Button
          size="xs"
          disabled={domains.length === 0}
          onClick={() => setCreating(true)}
          data-testid="add-hostname"
        >
          Register a name
        </Button>
      </Group>

      <Modal
        opened={creating}
        onClose={() => setCreating(false)}
        title="Register a name"
      >
        <Stack gap="sm">
          <Select
            label="Zone"
            description="The name has to sit inside this zone, or updates for it answer nohost."
            data={domains.map((d) => ({ value: String(d.id), label: d.name }))}
            value={zone}
            onChange={setZone}
            data-testid="hostname-zone"
          />
          <TextInput
            label="Name"
            description={
              selectedZone
                ? `The full name, including the zone — for example home.${selectedZone.name}`
                : 'The full name, including the zone.'
            }
            value={name}
            onChange={(event) => setName(event.currentTarget.value)}
            data-testid="hostname-name"
          />
          {willSend === '' ? null : (
            <Text size="xs" c="dimmed" data-testid="hostname-preview">
              {/* The exact bytes. The server lower-cases on the way in —
                  `/nic/update` looks the row up lower-cased, so an
                  upper-cased row would be unreachable — and refuses
                  anything else it would refuse on the wire. */}
              will send: <code>{willSend}</code> — stored lower-cased, because
              that is how <code>/nic/update</code> looks it up.
            </Text>
          )}
          <Select
            label="Device"
            description="Optional. A name can be registered now and assigned later, and it survives the device being deleted."
            data={deviceOptions(devices)}
            value={device}
            allowDeselect={false}
            onChange={(value) => setDevice(value ?? UNASSIGNED)}
            data-testid="hostname-device"
          />
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={willSend === '' || zone === null || busy}
              onClick={() =>
                zone === null
                  ? undefined
                  : create.mutate({
                      name: willSend,
                      domain_id: Number(zone),
                      device_id:
                        device === UNASSIGNED ? null : Number(device),
                    })
              }
              data-testid="hostname-submit"
            >
              Register
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={confirmDelete !== null}
        onClose={() => setConfirmDelete(null)}
        title="Delete this name?"
      >
        <Stack gap="sm">
          {/* Said before the button is pressed, not after. Deleting the
              row does not withdraw the record — that is what
              `/nic/delete` is for, and it needs the row in order to find
              the backend. */}
          <Text size="sm" data-testid="delete-hostname-warning">
            This removes the name from this service. It does{' '}
            <strong>not</strong> remove whatever your DNS provider has already
            published for it — that record stays in the zone with nothing
            pointing at it. Withdraw it first if you need it gone.
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={busy}
              onClick={() =>
                confirmDelete ? remove.mutate(confirmDelete.id) : undefined
              }
              data-testid="delete-hostname-confirm"
            >
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
