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
 *
 * ## Why delete asks in a second modal (#153)
 *
 * It used to ask **inline**, as an `Alert` in this form's own body. The
 * result was one surface carrying two sets of buttons and two meanings
 * of the word: the outer `Delete this name` *opened* the panel and the
 * inner `Delete w3.…` *performed* it, inches apart and reading almost
 * identically — with the form's own `Save` still live behind the
 * confirmation, so the surface offered *save* and *destroy* at the same
 * moment with no ordering between them.
 *
 * A destructive confirmation has to **interrupt**. Inline it is one
 * more panel on a busy form and can be scrolled past. This is the shape
 * `SecretOnceModal` established and `DeviceCard`'s own delete already
 * borrows: a second surface, `zIndex` above the modal it sits over,
 * that must be dismissed before the first is usable again. Reused
 * rather than reinvented — `zIndex={400}` is the same number for the
 * same reason, because Mantine gives every modal the same z-index and
 * siblings otherwise stack by mount order.
 *
 * Two consequences worth stating, because both are load-bearing:
 *
 * **The form beneath is locked, not merely covered.** `locked` disables
 * Save, Cancel and every other action while the confirmation is open.
 * The overlay stops a *mouse*, which is not the same as the action
 * being refused — a keyboard, an assistive technology or a test can all
 * reach a control an overlay is merely painted over.
 *
 * **The delete error renders inside the confirmation.** The form's own
 * error `Alert` is at the top of the body, behind the dialog: a failed
 * delete would have put its diagnosis on a surface nobody was looking
 * at. `DeviceCard` does the same thing for the same reason.
 *
 * ### The objection on the record, and the answer to it
 *
 * `ZoneModal.tsx` carries a comment arguing the opposite — *"a modal on
 * a modal is where the `Cancel` that deleted came from — two
 * overlapping dialogs, each with its own idea of what the buttons at
 * the bottom mean"*. That is a real incident and it is why `locked` is
 * not optional: **the hazard is two live `Cancel`s, not two dialogs.**
 * Here there is one interactive button row at a time — the form's
 * `Cancel` and `Save` are disabled while the confirmation is up — and
 * the dismissal is spelled *Keep it*, never *Cancel*, so no two
 * controls on screen share a word with two meanings.
 *
 * The comment is also **false about its own file**: `ZoneModal`'s
 * confirmation does not replace the button row, it renders above an
 * unconditional one, so `Delete this zone` / `Cancel` / `Save` all stay
 * live behind it — exactly the defect this issue describes, over a
 * larger blast radius (a zone, its stored provider credentials, and
 * every name under it). Out of scope for #153, reported with it.
 */
import { useState } from 'react';
import {
  ActionIcon,
  Alert,
  Button,
  CopyButton,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconCheck, IconCopy } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  HOSTNAMES_QUERY_KEY,
  updateHostname,
  createHostname,
  deleteHostname,
  hostnamesQuery,
  adoptZone,
  manualUpdate,
  publishingQuery,
  publishingQueryKey,
  setPublishing,
  type Hostname,
} from '../api/hostnames';
import { domainsQuery, type Domain } from '../api/domains';
import { devicesQuery, type Device } from '../api/devices';
import { CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';
import { BOARD_QUERY_KEY } from '../api/board';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { composeHostname, decomposeHostname } from './hostnameName';

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
  presetDeviceId = null,
}: {
  /** `null` is **create**. `opened` says whether anything is shown, so
   *  one value never has to mean both. */
  nameId: number | null;
  opened: boolean;
  onClose: () => void;
  /** Which device a *new* name starts attached to. Ignored when editing:
   *  the stored row is the truth there, and a URL that could override it
   *  would let a stale link silently reassign a name on open. */
  presetDeviceId?: number | null;
}) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={nameId === null ? 'Register a hostname' : 'Hostname'}
      size={640}
      {...CARD_MODAL_PROPS}
      styles={CARD_MODAL_STYLES}
      data-testid="name-modal"
    >
      {/* Portalled outside `[data-ddns-root]`; without this the whole
          form renders with none of `ddns.css`. */}
      <DdnsPortalScope>
        {opened ? (
          <NameModalBody
            key={nameId ?? 'new'}
            nameId={nameId}
            presetDeviceId={presetDeviceId}
            onClose={onClose}
          />
        ) : null}
      </DdnsPortalScope>
    </Modal>
  );
}

function NameModalBody({
  nameId,
  onClose,
  presetDeviceId,
}: {
  nameId: number | null;
  onClose: () => void;
  presetDeviceId: number | null;
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
    // Editing: the stored row wins, always. Creating: the caller's
    // preset, which the board's per-row `+` fills in from the row you
    // clicked — so adding a name to a device you are looking at does not
    // ask you which device you meant.
    setDevice(
      row
        ? row.device_id == null
          ? UNASSIGNED
          : String(row.device_id)
        : presetDeviceId === null
          ? UNASSIGNED
          : String(presetDeviceId),
    );
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
    // The board is the landing page and draws the same devices and
    // names. Invalidating only the list a surface happens to own leaves
    // the board showing rows that no longer exist — which is what a
    // create flow returning to the board looked like: a new device that
    // was not there until a manual reload.
    void client.invalidateQueries({ queryKey: HOSTNAMES_QUERY_KEY });
    void client.invalidateQueries({ queryKey: BOARD_QUERY_KEY });
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
  const adopt = useMutation({
    mutationFn: () => adoptZone(nameId as number),
    onSuccess: (r) => {
      setError(null);
      const now = r.adopted_v6 ?? r.adopted_v4 ?? '—';
      const was = r.previous_v6 ?? r.previous_v4 ?? 'nothing';
      setResult(`adopted ${now} — was ${was}. Nothing was published.`);
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
    // Deliberately **not** `onError: fail`. `fail` writes the form's own
    // error `Alert`, which lives at the top of this body — behind the
    // confirmation dialog. A failed delete would have put the server's
    // words on a surface nobody was looking at. It is rendered inside
    // the dialog instead, from `remove.error`.
  });

  const busy =
    create.isPending ||
    edit.isPending ||
    savePublishing.isPending ||
    publishNow.isPending ||
    remove.isPending;

  /** The confirmation is open, and there is a row for it to be about.
   *  `row` is in the condition because the dialog names the hostname. */
  const confirming = confirmDelete && row != null;

  /** What every control on the form beneath is disabled by.
   *
   *  Not decoration. Mantine's overlay stops a mouse; it does not stop a
   *  keyboard, an assistive technology or a test, and "cannot be saved
   *  while the confirmation is open" is a statement about the action,
   *  not about what is painted over what. */
  const locked = busy || confirming;

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
          next to it, and the `hostname:` line below already shows the
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
        <Group gap={6} align="center" wrap="nowrap">
          <Text>
            hostname: <span data-testid="hostname-will-send">{willSend}</span>
          </Text>
          {/* Copies the composed name and nothing else — not the label, not
            the zone select's value. It is the string a router is configured
            with, so it is the one thing on this form worth carrying to
            another window. */}
          <CopyButton value={willSend} timeout={2000}>
            {({ copied, copy }) => (
              <Tooltip label={copied ? 'Copied' : 'Copy the hostname'} withArrow>
                <ActionIcon
                  variant="subtle"
                  size="sm"
                  color={copied ? 'teal' : 'gray'}
                  onClick={copy}
                  aria-label="Copy the hostname"
                  data-testid="copy-hostname"
                >
                  {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                </ActionIcon>
              </Tooltip>
            )}
          </CopyButton>
        </Group>
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
                    disabled={locked}
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
                disabled={locked || publishIp.trim() === ''}
                onClick={() => publishNow.mutate(publishIp.trim())}
                data-testid="publish-now"
              >
                Publish now
              </Button>
              {/* Only when the zone disagrees. Publishing the zone's own
                value answers `nochg`, and `nochg` writes no `last_ip_*` by
                frozen rule — so without this there is no way to clear a
                divergence at all, and the row stays accented for ever. It
                calls no provider and spends no rate-limit slot. */}
              {publishing.data?.zone_differs ? (
                <Button
                  size="xs"
                  variant="default"
                  disabled={locked}
                  onClick={() => adopt.mutate()}
                  data-testid="adopt-zone"
                >
                  Adopt {publishing.data.zone_differs}
                </Button>
              ) : null}
            </Group>
            {result ? (
              <Text size="xs" ff="monospace" data-testid="publish-result">
                {result}
              </Text>
            ) : null}
          </Stack>
        </>
      )}

      {/* Its own surface, over this one. `zIndex` is the number
          `SecretOnceModal` uses and for the identical reason: this modal
          has to outrank a modal that is already open, and Mantine gives
          every modal the same z-index, so siblings stack by mount order
          until a re-render changes it.

          The testid is on the body rather than on `Modal`, because
          Mantine does not forward `data-testid` to a node a DOM query
          can reach — `deviceCard.test.tsx` records the same. */}
      <Modal
        opened={confirming}
        onClose={() => {
          remove.reset();
          setConfirmDelete(false);
        }}
        title="Delete this name?"
        zIndex={400}
      >
        {/* Portalled to `document.body`, outside `[data-ddns-root]`, so
            without this the dialog renders with none of `ddns.css` — and
            `.ddns-data` is what sets the hostname apart from the prose
            around it. */}
        <DdnsPortalScope>
          <Stack gap="sm" data-testid="name-delete-confirm">
            {/* Verbatim, and it stays verbatim. This is the sentence
                users get wrong: deleting here does not un-publish
                anything, and the record keeps answering with nothing
                left to maintain it. */}
            <Text size="sm">
              This removes{' '}
              <span className="ddns-data">{row?.name}</span> from this
              service. It does <strong>not</strong> remove the record your
              provider has already published — that stays in the zone with
              nothing maintaining it.
            </Text>
            {remove.error ? (
              <Alert color="gray" variant="light" data-testid="name-delete-error">
                <Text size="sm" ff="monospace">
                  {(remove.error as Error).message}
                </Text>
              </Alert>
            ) : null}
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => {
                  remove.reset();
                  setConfirmDelete(false);
                }}
                data-testid="name-delete-keep"
              >
                Keep it
              </Button>
              <Button
                size="xs"
                color="red"
                disabled={busy}
                onClick={() => remove.mutate()}
                data-testid="name-delete-confirmed"
              >
                Delete {row?.name}
              </Button>
            </Group>
          </Stack>
        </DdnsPortalScope>
      </Modal>

      <Group justify="space-between">
        {creating ? (
          <span />
        ) : (
          <Button
            size="xs"
            variant="default"
            disabled={locked}
            onClick={() => {
              remove.reset();
              setConfirmDelete(true);
            }}
            data-testid="name-delete"
          >
            Delete this name
          </Button>
        )}
        <Group gap="sm">
          <Button size="xs" variant="default" disabled={locked} onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="xs"
            disabled={locked || (creating && (willSend === '' || zone === null))}
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
