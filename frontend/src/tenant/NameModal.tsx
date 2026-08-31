/** One modal for a name — creating one, and every setting on an
 *  existing one.
 *
 * ## Why there is only one
 *
 * There were three: a create form, a "Publishing" dialog behind a gear,
 * and an assign-device dropdown living in the list row. Three places to
 * learn one object, and the row dropdown meant the list could mutate
 * data while looking like a list.
 *
 * ## Why the provider checkboxes are gone
 *
 * They listed the zone's provider bindings so a name could publish to a
 * subset. That was built when a zone could have several; a zone has
 * **one** provider now, so the control was a checkbox list of one item —
 * and on a compat-fixture zone it rendered `stub1` / `stub2`, internal
 * slot names that mean nothing to anyone.
 *
 * The setting still exists in the schema and on the wire. It is shown
 * **only when the zone actually has more than one binding**, which the
 * UI can no longer create and only a legacy import produces — because
 * hiding it there would leave a stored value nobody could see or undo.
 *
 * ## What cannot be edited, and why the form says so
 *
 * The name and its zone are fixed after creation. A hostname *is* the
 * DNS name: it is what `/nic/update` looks the row up by, and what a
 * provider has already published a record under. `HostnameAssignIn`
 * carries `device_id` and nothing else for exactly that reason. So both
 * are rendered as values with the reason attached, rather than as
 * disabled inputs that look like a permissions problem.
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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  HOSTNAMES_QUERY_KEY,
  updateHostname,
  createHostname,
  deleteHostname,
  hostnamesQuery,
  manualUpdate,
  publishingQuery,
  publishingQueryKey,
  setPublishing,
  type Hostname,
} from '../api/hostnames';
import { domainsQuery, type Domain } from '../api/domains';
import { devicesQuery, type Device } from '../api/devices';
import { CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { composeHostname, decomposeHostname } from './HostnameList';

/** The `value` a Mantine `Select` uses for *no device*. `Select` speaks
 *  strings and `null` clears it, so unassigned needs a sentinel. */
const UNASSIGNED = 'none';

function deviceOptions(devices: Device[]) {
  return [
    { value: UNASSIGNED, label: 'Not assigned' },
    ...devices.map((d) => ({ value: String(d.id), label: d.name })),
  ];
}

export function NameModal({
  nameId,
  opened,
  onClose,
}: {
  /** `null` is **create**. `opened` says whether anything is shown, so
   *  one value never has to mean both. */
  nameId: number | null;
  opened: boolean;
  onClose: () => void;
}) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={nameId === null ? 'Register a name' : 'Name'}
      size={640}
      {...CARD_MODAL_PROPS}
      styles={CARD_MODAL_STYLES}
      data-testid="name-modal"
    >
      {/* Portalled outside `[data-ddns-root]`; without this the whole
          form renders with none of `ddns.css`. */}
      <DdnsPortalScope>
        {opened ? (
          <NameModalBody key={nameId ?? 'new'} nameId={nameId} onClose={onClose} />
        ) : null}
      </DdnsPortalScope>
    </Modal>
  );
}

function NameModalBody({
  nameId,
  onClose,
}: {
  nameId: number | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const hostnames = useQuery(hostnamesQuery({ enabled: true }));
  const domains = useQuery(domainsQuery({ enabled: true }));
  const devices = useQuery(devicesQuery({ enabled: true }));
  const publishing = useQuery(publishingQuery(nameId));

  const creating = nameId === null;
  const row: Hostname | undefined = creating
    ? undefined
    : hostnames.data?.find((h) => h.id === nameId);

  /* Nothing is seeded by a `useState` initialiser. On a cold load the
     queries have not resolved on the first render, and an initialiser
     runs exactly once — which is how the zone modal came up with an
     empty name box on a zone that plainly had one. */
  const [seededFrom, setSeededFrom] = useState<number | null | undefined>(
    undefined,
  );
  const [typed, setTyped] = useState('');
  const [zone, setZone] = useState<string | null>(null);
  const [device, setDevice] = useState<string>(UNASSIGNED);
  const [ttl, setTtl] = useState('');
  const [publishIp, setPublishIp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const ready =
    !hostnames.isLoading &&
    !domains.isLoading &&
    !devices.isLoading &&
    (creating || !publishing.isLoading);
  const identity = row ? row.id : creating ? null : undefined;
  if (ready && identity !== undefined && seededFrom !== identity) {
    setSeededFrom(identity);
    setDevice(row?.device_id == null ? UNASSIGNED : String(row.device_id));
    setZone(row ? String(row.domain_id) : null);
    // The label, with the zone suffix removed — the composer puts it
    // back. Seeded from the row so an edit starts from what is stored
    // rather than from an empty box.
    if (row) {
      const z = domains.data?.find((d) => d.id === row.domain_id)?.name ?? '';
      // `decomposeHostname`, not a second `endsWith` here: the suffix
      // decision has one home, and this is the inverse of the composer
      // the submit path uses.
      setTyped(decomposeHostname(row.name, z));
    }
    setTtl(publishing.data?.ttl == null ? '' : String(publishing.data.ttl));
  }

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: HOSTNAMES_QUERY_KEY });
    if (nameId !== null) {
      void client.invalidateQueries({ queryKey: publishingQueryKey(nameId) });
    }
  };
  const fail = (e: Error) => setError(e.message);

  const create = useMutation({
    mutationFn: createHostname,
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: fail,
  });
  const edit = useMutation({
    mutationFn: (body: {
      device_id: number | null;
      name?: string;
      domain_id?: number;
    }) => updateHostname(nameId as number, body),
    onError: fail,
  });
  const savePublishing = useMutation({
    mutationFn: (body: { backend_ids: number[] | null; ttl: number | null }) =>
      setPublishing(nameId as number, body),
    onError: fail,
  });
  const publishNow = useMutation({
    mutationFn: (ip: string) => manualUpdate(nameId as number, ip),
    onSuccess: (r) => {
      setError(null);
      setResult(
        `${r.status}${r.published ? '' : ' — nothing published'} · ${r.attempts
          .map((a) => `${a.backend_type} ${a.status}`)
          .join(', ')}`,
      );
      invalidate();
    },
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: () => deleteHostname(nameId as number),
    onSuccess: () => {
      invalidate();
      onClose();
    },
    onError: fail,
  });

  const busy =
    create.isPending ||
    edit.isPending ||
    savePublishing.isPending ||
    publishNow.isPending ||
    remove.isPending;

  if (!ready) return <Text size="sm">Loading…</Text>;
  if (!creating && !row) {
    return (
      <Alert color="gray" variant="light" title="No such name" data-testid="name-missing">
        <Text size="sm">
          Name <code>{nameId}</code> is not one of yours, or it has been deleted.
        </Text>
      </Alert>
    );
  }

  const zoneList: Domain[] = domains.data ?? [];
  const selectedZone = zoneList.find((d) => String(d.id) === zone) ?? null;
  const willSend = composeHostname(typed, selectedZone?.name ?? null);
  const pub = publishing.data;

  /** Save is **two** requests — the row (`PATCH /hostnames/{id}`) and its
   *  publishing settings (`PUT .../backends`) are different endpoints —
   *  and it has to behave like one action: both land, then the list
   *  refreshes and the modal closes.
   *
   *  Neither mutation carries `onSuccess` for that reason. Per-mutation
   *  success handlers would close the modal when the *first* finished,
   *  so a failing TTL would be reported onto a dialog that had already
   *  gone. `mutateAsync` sequences them and one exit runs at the end.
   *
   *  This is why the list did not update and the modal did not close:
   *  both mutations had `onError` and nothing else, so the success path
   *  did not exist at all. */
  const saveEdits = async () => {
    setError(null);
    const wanted = device === UNASSIGNED ? null : Number(device);
    // Name and zone travel together so the server validates the new
    // label against the zone it is moving *to*, not against a state that
    // never exists.
    const n = ttl.trim() === '' ? null : Number(ttl.trim());
    // `undefined` would be "omit", which the API reads the same as
    // `null`; passing the current state explicitly keeps Save from
    // silently un-pinning an imported row the operator has not touched.
    try {
      await edit.mutateAsync({
        device_id: wanted,
        ...(willSend !== '' && willSend !== row?.name ? { name: willSend } : {}),
        ...(zone !== null && Number(zone) !== row?.domain_id
          ? { domain_id: Number(zone) }
          : {}),
      });
      await savePublishing.mutateAsync({
        backend_ids: pub && !pub.inherits_backends ? pub.publishes_to : null,
        ttl: n,
      });
    } catch {
      // `onError` on each mutation has already put the server's own
      // words on screen. Swallowed here only so the rejection does not
      // escape as an unhandled promise; the modal stays open, which is
      // the right place to be when a save failed.
      return;
    }
    invalidate();
    onClose();
  };

  return (
    <Stack gap="md" data-testid="name-modal-body">
      {error ? (
        <Alert color="gray" variant="light" title="That did not work" data-testid="name-error">
          <Text size="sm" ff="monospace">{error}</Text>
        </Alert>
      ) : null}

      {/* The same fields in both modes. There is no read-only variant of
          this form any more: a name and a zone that could be set once
          and never corrected made a typo permanent and pushed people
          into delete-and-recreate — which destroys the published-address
          history the board draws and leaves the identical orphaned record
          at the provider. The consequence is stated below instead. */}
      {/* Name first, then zone — the order the value reads in. The
          trailing `.zone` echo is gone: it repeated the select sitting
          next to it, and the `will send:` line below already shows the
          composed result, which is the thing that actually leaves the
          browser. */}
      <Group gap="sm" align="flex-end" wrap="nowrap">
        <TextInput
          label="Name"
          value={typed}
          onChange={(e) => setTyped(e.currentTarget.value)}
          data-testid="hostname-name"
          styles={{ input: { width: '25ch' } }}
        />
        <Select
          label="Zone"
          data={zoneList.map((d) => ({ value: String(d.id), label: d.name }))}
          value={zone}
          onChange={setZone}
          data-testid="hostname-zone"
          w={240}
        />
      </Group>
      {willSend === '' ? null : (
        <Text size="xs" c="dimmed">
          will send: <code data-testid="hostname-will-send">{willSend}</code>
        </Text>
      )}
      {/* Only when there is something to orphan. The note was
          unconditional, so it fired on a name that had never published —
          where it was simply untrue: nothing is left behind because
          nothing was ever put there. `last_updated_at` is the server's
          own record of whether a `good` aggregate has ever landed. */}
      {!creating &&
      willSend !== '' &&
      willSend !== row?.name &&
      row?.last_updated_at != null ? (
        <Alert
          color="gray"
          variant="light"
          title="This name has already published"
          data-testid="name-rename-warning"
        >
          <Text size="sm">
            <span className="ddns-data">{row?.name}</span> becomes{' '}
            <span className="ddns-data">{willSend}</span>. The record already
            published under the old name stays in the zone with nothing
            maintaining it — this service stops updating it, and it does not
            remove it.
          </Text>
        </Alert>
      ) : null}

      <Group gap="sm" align="center" wrap="nowrap">
        <span className="ddns-th" style={{ minWidth: '7rem' }}>
          Device
        </span>
        <Select
          aria-label="Device"
          data={deviceOptions(devices.data ?? [])}
          value={device}
          allowDeselect={false}
          onChange={(v) => setDevice(v ?? UNASSIGNED)}
          data-testid="hostname-device"
          w={240}
        />
      </Group>

      {creating ? null : (
        <>
          <Group align="center" gap="sm" wrap="nowrap">
            {/* Label beside the control, matching Device. Mantine stacks
                `label` above the input, so it is rendered as its own element
                and the input keeps an `aria-label` — otherwise the field
                loses its accessible name along with its visible one. */}
            <span className="ddns-th" style={{ minWidth: '7rem' }}>
              TTL override
            </span>
            <TextInput
              size="xs"
              aria-label="TTL override"
              value={ttl}
              placeholder="inherit"
              inputMode="numeric"
              maxLength={8}
              onChange={(e) => setTtl(e.currentTarget.value)}
              data-testid="publishing-ttl"
              styles={{ input: { width: '9ch' } }}
            />
          </Group>

          {/* Not a choice. A name lives in a zone and the zone has a
              provider; there is nothing for the operator to pick, and a
              checkbox list of the zone's bindings was asking a question
              the model does not pose.

              It is still *state*, because the engine aggregates across N
              bindings — a legacy requirement, frozen in the wire table
              (`mixed` 2, `allnochg` 2, `firsterr` 3) and reachable by
              import. So a name that an import left pinned to a subset
              says so and offers the one operation that returns it to the
              normal model. Read-only, not a matrix. */}
          {pub && !pub.inherits_backends ? (
            <Alert
              color="gray"
              variant="light"
              title="This name does not follow its zone"
              data-testid="name-pinned-backends"
            >
              <Stack gap="sm">
                <Text size="sm">
                  An import pinned it to{' '}
                  <strong>
                    {pub.backends
                      .filter((b) => pub.publishes_to.includes(b.backend_id))
                      .map((b) => b.backend_type)
                      .join(', ') || 'nothing'}
                  </strong>
                  , so a provider added to the zone later will not publish it.
                  Normally a name follows its zone.
                </Text>
                <Group justify="flex-end">
                  <Button
                    size="xs"
                    variant="default"
                    disabled={busy}
                    onClick={() =>
                      savePublishing.mutate({
                        // `null` restores *inherit*. `[]` means the same
                        // request to the API and both undo the pinning.
                        backend_ids: null,
                        ttl: ttl.trim() === '' ? null : Number(ttl.trim()),
                      })
                    }
                    data-testid="name-follow-zone"
                  >
                    Follow the zone
                  </Button>
                </Group>
              </Stack>
            </Alert>
          ) : null}

          <Stack gap={4}>
            <span className="ddns-th">Publish now</span>
            <Text size="xs" c="dimmed">
              Contacts the provider immediately and spends one of the device's
              rate-limit slots — the same budget its router uses.
            </Text>
            <Group gap="sm" align="flex-end">
              <TextInput
                size="xs"
                aria-label="Address to publish"
                placeholder="203.0.113.10"
                value={publishIp}
                onChange={(e) => setPublishIp(e.currentTarget.value)}
                data-testid="publish-ip"
                styles={{ input: { width: '22ch' } }}
              />
              <Button
                size="xs"
                variant="default"
                disabled={busy || publishIp.trim() === ''}
                onClick={() => publishNow.mutate(publishIp.trim())}
                data-testid="publish-now"
              >
                Publish now
              </Button>
            </Group>
            {result ? (
              <Text size="xs" ff="monospace" data-testid="publish-result">
                {result}
              </Text>
            ) : null}
          </Stack>
        </>
      )}

      {confirmDelete && row ? (
        <Alert color="gray" variant="light" title="Delete this name?" data-testid="name-delete-confirm">
          <Stack gap="sm">
            <Text size="sm">
              This removes <span className="ddns-data">{row.name}</span> from
              this service. It does <strong>not</strong> remove the record your
              provider has already published — that stays in the zone with
              nothing maintaining it.
            </Text>
            <Group justify="flex-end">
              <Button size="xs" variant="default" disabled={busy} onClick={() => setConfirmDelete(false)}>
                Keep it
              </Button>
              <Button size="xs" color="red" disabled={busy} onClick={() => remove.mutate()} data-testid="name-delete-confirmed">
                Delete {row.name}
              </Button>
            </Group>
          </Stack>
        </Alert>
      ) : null}

      <Group justify="space-between">
        {creating ? (
          <span />
        ) : (
          <Button size="xs" variant="default" disabled={busy} onClick={() => setConfirmDelete(true)} data-testid="name-delete">
            Delete this name
          </Button>
        )}
        <Group gap="sm">
          <Button size="xs" variant="default" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="xs"
            disabled={busy || (creating && (willSend === '' || zone === null))}
            onClick={() =>
              creating
                ? create.mutate({
                    name: willSend,
                    domain_id: Number(zone),
                    device_id: device === UNASSIGNED ? null : Number(device),
                  })
                : saveEdits()
            }
            data-testid="name-submit"
          >
            {creating ? 'Register' : 'Save'}
          </Button>
        </Group>
      </Group>
    </Stack>
  );
}
