/** `/atrium-ddns/devices/:id` — `docs/ops/ui-design.md` §11.2.
 *
 * ## Why this is a route
 *
 * §12 settles it on the width budget, not on preference. One resolution
 * strip needs **≈592px** (§3.1); atrium's shell gives **1168px** at a
 * 1440px viewport (§3.6). A master/detail split at a conventional
 * 360/800 leaves **~790px** — one strip up, never two, on a surface
 * whose whole job is comparing names. A right drawer at Mantine's `lg`
 * is **620px**, which is *below the one-strip minimum*: the signature
 * element would wrap inside its own detail view. A route keeps the full
 * 1168px, is linkable into a ticket, and leaves the back button working.
 *
 * ## Why the name is edited in place
 *
 * It is one short string with one failure mode. A modal is heavier than
 * the edit — it takes a click to open, a click to dismiss, and it hides
 * the thing being renamed behind an overlay while you rename it. The
 * pencil swaps the heading for an input at the same position; Escape and
 * Cancel put it back.
 *
 * ## The three things this page must not get wrong
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
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Anchor,
  Button,
  Divider,
  Group,
  Modal,
  NumberInput,
  Radio,
  Stack,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import { boardQuery } from './api/board';
import {
  DEVICES_QUERY_KEY,
  DEVICE_PERMISSION,
  deviceQuery,
  renameDevice,
  rotateDeviceSecret,
  updateDeviceLimit,
  type Device,
  type DeviceSecret,
} from './api/devices';
import { ApiError } from './api/http';
import { HostnameBlock } from './board/DeviceBoard';
import { absoluteTitle, formatAge, rateLimitSummary } from './board/format';
import { DdnsRoot } from './host/DdnsRoot';
import { DEVICES_PATH, deviceIdFromPath } from './paths';
import { MigratedNotice, SecretOnce } from './tenant/SecretOnce';

/** The id in the address bar.
 *
 * `window.location`, not `useParams`: the host bundle mounts its own
 * React tree inside atrium's, so react-router's context does not cross
 * the boundary and importing react-router here would give this tree a
 * *second* router with its own idea of the location. Same decision, and
 * the same reasoning, as `logs/useLogQuery.ts`.
 *
 * Guarded for a non-browser host — a throw at module scope would take
 * the whole bundle down at import time.
 */
function useDeviceId(): number | null {
  const read = () =>
    typeof window === 'undefined'
      ? null
      : deviceIdFromPath(window.location.pathname);
  const [id, setId] = useState<number | null>(read);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onPop = () => setId(read());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return id;
}

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
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(device.name);
  const [refusal, setRefusal] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: renameDevice,
    onSuccess: (next) => {
      setEditing(false);
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

  const start = () => {
    setDraft(device.name);
    setRefusal(null);
    setEditing(true);
  };

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

  if (!editing) {
    return (
      <Stack gap={4}>
        <Group gap="sm" align="baseline">
          <Title order={3} className="ddns-data" data-testid="device-name">
            {device.name}
          </Title>
          <Button
            size="compact-xs"
            variant="subtle"
            onClick={start}
            data-testid="device-rename"
          >
            Rename
          </Button>
        </Group>
      </Stack>
    );
  }

  return (
    <Stack gap="xs">
      <Group gap="sm" align="flex-end">
        <TextInput
          label="Name"
          description="What you call it. The username and the secret are not affected."
          value={draft}
          autoFocus
          disabled={rename.isPending}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') submit();
            if (event.key === 'Escape') setEditing(false);
          }}
          data-testid="device-name-input"
        />
        <Button
          size="xs"
          disabled={draft.trim() === '' || rename.isPending}
          onClick={submit}
          data-testid="device-name-save"
        >
          Save
        </Button>
        <Button
          size="xs"
          variant="default"
          disabled={rename.isPending}
          onClick={() => setEditing(false)}
          data-testid="device-name-cancel"
        >
          Cancel
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
  const [inherit, setInherit] = useState(device.rate_limit_per_minute === null);
  const [value, setValue] = useState<number | ''>(
    device.rate_limit_per_minute === null ? '' : device.rate_limit_per_minute,
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

  return (
    <Stack gap="xs">
      <span className="ddns-label">rate limit</span>
      <Group gap="lg" align="flex-end">
        <NumberInput
          label="Updates per minute"
          value={value}
          min={0}
          disabled={inherit || save.isPending}
          onChange={(next) => setValue(typeof next === 'number' ? next : '')}
          data-testid="detail-limit-input"
        />
        <Radio.Group
          value={inherit ? 'inherit' : 'own'}
          onChange={(next) => setInherit(next === 'inherit')}
          data-testid="detail-limit-choice"
        >
          <Stack gap={4}>
            <Radio
              value="own"
              label="set on this device"
              data-testid="detail-limit-own"
            />
            <Radio
              value="inherit"
              /* The number is named only when it is knowable. With a
                 per-device value set, the installation default lives
                 behind `app_setting.manage` and this browser does not
                 hold it — a number here would be one the page invented.
                 `n/a` is never `0`, and it is never a plausible 30
                 either. */
              label={
                device.rate_limit_per_minute === null
                  ? `inherit the installation default (${device.effective_rate_limit_per_minute})`
                  : 'inherit the installation default'
              }
              data-testid="detail-limit-inherit"
            />
          </Stack>
        </Radio.Group>
        <Button
          size="xs"
          disabled={save.isPending}
          onClick={() =>
            save.mutate({
              id: device.id,
              // `null` is the *inherit* choice, made explicitly. `0`
              // is a different state — may never call — and reachable
              // only by typing it.
              rate_limit_per_minute: inherit ? null : value === '' ? null : value,
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

export function DeviceDetailInner() {
  const client = useQueryClient();
  const id = useDeviceId();
  const hasPerm = usePerm();
  const canRead = hasPerm(DEVICE_PERMISSION);

  const { data, isLoading, error } = useQuery(
    deviceQuery(id, { enabled: canRead }),
  );
  // The strips arrive from the board, computed. Every verdict on them —
  // the five `DnsCheckStatus` values, both joint verdicts, the collapse
  // denominator — is decided server-side, and fetching them from a
  // second endpoint that rebuilt them would be the two-implementations
  // defect `api/board.ts` opens by forbidding.
  const board = useQuery(boardQuery({ enabled: canRead && id !== null }));

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
    board.data?.devices.find((entry) => entry.id === id)?.hostnames ?? [];

  return (
    <Stack gap="md">
      <Anchor href={DEVICES_PATH} size="sm" data-testid="detail-back">
        {/* A real anchor, for `DeviceBoardPage`'s reason: react-router's
            `Link` is not reachable from this tree and a bare `pushState`
            would move the address bar without telling atrium's router. */}
        ← devices
      </Anchor>

      {!canRead ? (
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
      ) : id === null ? (
        <Alert
          color="gray"
          variant="light"
          title="That is not a device address"
          data-testid="detail-bad-url"
        >
          <Text size="sm">
            <code>{DEVICES_PATH}/&lt;id&gt;</code> expects a numeric id. Go back
            to the device list and follow a row.
          </Text>
        </Alert>
      ) : isLoading ? (
        <Text size="sm" data-testid="detail-loading">
          Loading…
        </Text>
      ) : error ? (
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
      ) : data ? (
        <Stack gap="lg" data-testid="device-detail">
          <Stack gap={4}>
            <DeviceName device={data} onSaved={refresh} />
            <Group gap="md">
              <span className="ddns-label" data-testid="detail-username">
                {data.username}
              </span>
              <span
                className="ddns-station__time"
                title={absoluteTitle(data.last_seen_at)}
                data-testid="detail-last-seen"
              >
                seen {formatAge(data.last_seen_at)}
              </span>
              <span
                className="ddns-station__time"
                title={absoluteTitle(data.created_at)}
                data-testid="detail-created"
              >
                created {absoluteTitle(data.created_at)}
              </span>
            </Group>
          </Stack>

          <MigratedNotice origin={data.credential_origin} />

          <RateLimit device={data} onSaved={refresh} />

          <Stack gap="xs">
            <span className="ddns-label">names this device updates</span>
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
            <span className="ddns-label">credential</span>
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
            <Group gap="sm">
              <Text size="sm" data-testid="detail-rotate-consequence">
                Rotate the secret — the device stops working until it is
                reconfigured.
              </Text>
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
      ) : null}
    </Stack>
  );
}

export function DeviceDetailPage() {
  return (
    <DdnsRoot>
      <DeviceDetailInner />
    </DdnsRoot>
  );
}
