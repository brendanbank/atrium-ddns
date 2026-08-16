/** Publishing — the legacy `/admin/hostnames/<id>/backends` page.
 *
 * Three controls that had no schema behind them until `0004`: which
 * provider bindings this name publishes to, what TTL it is published
 * at, and a button that publishes an address now instead of waiting for
 * the router.
 *
 * ## The rendering rule this file exists to hold
 *
 * **An empty selection means *every backend on the zone*, not *none*.**
 * That is the migration's whole safety property — every hostname that
 * existed before `0004` has no selection rows — and it is trivially
 * easy to render as its opposite: a set of unticked checkboxes reads as
 * "publishes to nothing" to anyone who has not read the migration.
 *
 * So the checkboxes are never the whole answer. Above them sits a line
 * naming what the server resolved (`publishes_to`), and when nothing is
 * ticked it says *inheriting* in as many words rather than leaving a
 * blank to be interpreted. The two facts come from the server as two
 * fields; this file computes neither.
 *
 * ## The TTL has three levels and `null` is not 60
 *
 * Empty means *inherit*, and inherit resolves to the binding, and the
 * binding falls back to the service default. The number a name is
 * actually published at (`effective_ttl`) is shipped per binding and
 * shown beside the input, because "60" typed in and "60 inherited" are
 * different rows and only one of them follows a later change to the
 * zone.
 *
 * ## The manual update is metered, and says so
 *
 * It draws on the device's rate-limit budget — the same one the router
 * draws on — so the button reports the limit it spent against, and a
 * `429` is rendered as the server's own sentence rather than as a
 * generic failure. The per-backend results are shown individually: an
 * aggregate `good` over three backends of which one answered `dnserr`
 * is the state an owner most needs to see, and it is the one an
 * aggregate alone hides.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Group,
  Modal,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  HOSTNAMES_QUERY_KEY,
  manualUpdate,
  publishingQuery,
  publishingQueryKey,
  setPublishing,
  type Hostname,
  type ManualUpdateResult,
} from '../api/hostnames';
import { responseGlyph, responseTone, responseWord } from '../logs/format';

function ttlToInput(ttl: number | null): string {
  return ttl === null ? '' : String(ttl);
}

export function HostnamePublishingModal({
  hostname,
  onClose,
}: {
  hostname: Hostname | null;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const id = hostname?.id ?? null;
  const publishing = useQuery(publishingQuery(id));

  const [chosen, setChosen] = useState<number[]>([]);
  const [ttl, setTtl] = useState('');
  const [ip, setIp] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ManualUpdateResult | null>(null);

  const data = publishing.data;

  // Seed the form from the server's state whenever a different name is
  // opened or the server's answer changes. Keyed on the hostname id as
  // well as the payload so re-opening the same modal after a save does
  // not silently keep a stale local edit.
  useEffect(() => {
    if (!data) return;
    setChosen(
      data.backends.filter((b) => b.selected).map((b) => b.backend_id),
    );
    setTtl(ttlToInput(data.ttl));
  }, [data, id]);

  useEffect(() => {
    setError(null);
    setResult(null);
    setIp('');
  }, [id]);

  const invalidate = () => {
    if (id === null) return;
    void client.invalidateQueries({ queryKey: publishingQueryKey(id) });
    void client.invalidateQueries({ queryKey: HOSTNAMES_QUERY_KEY });
  };

  const save = useMutation({
    mutationFn: () =>
      setPublishing(id as number, {
        // `[]` and `null` are the same request server-side; `[]` is sent
        // because it is what the checkboxes literally say.
        backend_ids: chosen,
        ttl: ttl.trim() === '' ? null : Number(ttl.trim()),
      }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err: Error) => setError(err.message),
  });

  const publish = useMutation({
    mutationFn: () => manualUpdate(id as number, ip.trim()),
    onSuccess: (outcome) => {
      setError(null);
      setResult(outcome);
      invalidate();
    },
    onError: (err: Error) => {
      setResult(null);
      setError(err.message);
    },
  });

  const busy = save.isPending || publish.isPending;
  const ttlNumber = ttl.trim() === '' ? null : Number(ttl.trim());
  const ttlOutOfRange =
    data !== undefined &&
    ttlNumber !== null &&
    (!Number.isInteger(ttlNumber) ||
      ttlNumber < data.ttl_min ||
      ttlNumber > data.ttl_max);

  return (
    <Modal
      opened={hostname !== null}
      onClose={onClose}
      title={hostname ? `Publishing — ${hostname.name}` : 'Publishing'}
      size="lg"
    >
      <Stack gap="sm">
        {error ? (
          <Alert
            color="gray"
            variant="light"
            title="That did not work"
            data-testid="publishing-error"
          >
            {/* The server's own words, in full — including a 429's
                explanation of which budget was spent. */}
            <Text size="sm" ff="monospace">
              {error}
            </Text>
          </Alert>
        ) : null}

        {publishing.isLoading ? (
          <Text size="sm" data-testid="publishing-loading">
            Loading…
          </Text>
        ) : publishing.error ? (
          <Alert
            color="gray"
            variant="light"
            title="Could not load this name's publishing settings"
            data-testid="publishing-load-error"
          >
            <Text size="sm" ff="monospace">
              {(publishing.error as Error).message}
            </Text>
          </Alert>
        ) : data ? (
          <>
            {/* The resolved answer, first and in words. Everything below
                is the stored state; this is the effect, and the two are
                not the same when nothing is ticked. */}
            <Text size="sm" data-testid="publishing-summary">
              {data.publishes_to.length === 0
                ? 'This name publishes to nothing: its zone has no provider bindings, so updates for it answer 911.'
                : data.inherits_backends
                  ? `Inheriting the zone — publishes to all ${data.publishes_to.length} of ${data.domain_name}'s provider bindings. Adding a binding to the zone adds it here too.`
                  : `Publishes to ${data.publishes_to.length} of ${data.backends.length} bindings on ${data.domain_name}.`}
            </Text>

            {data.backends.length === 0 ? (
              <Text size="sm" data-testid="publishing-no-backends">
                {data.domain_name} has no provider bindings. Add one on the
                zones page — a name with nothing to publish to answers 911.
              </Text>
            ) : (
              <Stack gap={4}>
                {data.backends.map((backend) => (
                  <Checkbox
                    key={backend.backend_id}
                    size="xs"
                    disabled={busy}
                    checked={chosen.includes(backend.backend_id)}
                    data-testid={`publish-to-${backend.backend_type}`}
                    label={
                      <Text
                        size="sm"
                        data-testid={`publish-label-${backend.backend_type}`}
                      >
                        {backend.backend_type}
                        <Text component="span" size="xs" c="dimmed">
                          {' '}
                          — publishes at {backend.effective_ttl}s
                          {backend.credentials_set
                            ? ''
                            : ' · no credentials stored, so this one answers 911'}
                        </Text>
                      </Text>
                    }
                    onChange={(event) =>
                      setChosen((current) =>
                        event.currentTarget.checked
                          ? [...current, backend.backend_id]
                          : current.filter((x) => x !== backend.backend_id),
                      )
                    }
                  />
                ))}
                <Text size="xs" c="dimmed" data-testid="publishing-empty-note">
                  Ticking none is not "publish nowhere" — it means follow the
                  zone, which is what every name does until it is changed here.
                </Text>
              </Stack>
            )}

            <TextInput
              size="xs"
              label="TTL override"
              description={`Seconds, ${data.ttl_min}–${data.ttl_max}. Leave empty to inherit — that resolves to ${data.default_ttl}s unless the zone's binding says otherwise, and it follows a later change to it. A number typed here does not.`}
              value={ttl}
              disabled={busy}
              placeholder="inherit"
              error={
                ttlOutOfRange
                  ? `Between ${data.ttl_min} and ${data.ttl_max} seconds, or empty to inherit.`
                  : undefined
              }
              onChange={(event) => setTtl(event.currentTarget.value)}
              data-testid="publishing-ttl"
            />

            <Group justify="flex-end">
              <Button
                size="xs"
                disabled={busy || ttlOutOfRange}
                onClick={() => save.mutate()}
                data-testid="publishing-save"
              >
                Save
              </Button>
            </Group>

            <Text size="sm" fw={600}>
              Publish now
            </Text>
            <Text size="xs" c="dimmed" data-testid="publishing-update-note">
              {/* Said before the button is pressed. This reaches the
                  provider, costs whatever the provider charges, and
                  spends a slot from the device's per-minute budget —
                  the same budget the router draws on. */}
              This contacts the provider immediately and spends one of{' '}
              {hostname?.device_name ?? 'this name’s device'}’s rate-limit
              slots — the same budget its router uses.
            </Text>
            <Group align="flex-end" gap="xs">
              <TextInput
                size="xs"
                label="Address"
                description="The address to publish. Not defaulted to yours — this is the router's address, not the browser's."
                placeholder="203.0.113.10"
                value={ip}
                disabled={busy || hostname?.device_id == null}
                onChange={(event) => setIp(event.currentTarget.value)}
                data-testid="publishing-ip"
              />
              <Button
                size="xs"
                variant="default"
                disabled={busy || ip.trim() === '' || hostname?.device_id == null}
                onClick={() => publish.mutate()}
                data-testid="publishing-update"
              >
                Publish now
              </Button>
            </Group>
            {hostname?.device_id == null ? (
              <Text size="xs" c="dimmed" data-testid="publishing-no-device">
                Assign a device first. A manual update is charged to the
                device’s rate-limit budget and attributed to it in the log, and
                an unassigned name has neither.
              </Text>
            ) : null}

            {result ? (
              <Alert
                color="gray"
                variant="light"
                title={`Answered ${result.status}`}
                data-testid="publishing-result"
              >
                <Stack gap={2}>
                  <Text size="sm">
                    {result.published
                      ? `Published ${result.ip} as ${result.rtype}.`
                      : `Nothing was written: ${result.status} for ${result.ip} (${result.rtype}).`}
                  </Text>
                  {/* Per backend, not just the aggregate. A `good`
                      aggregate over three backends of which one answered
                      `dnserr` is exactly the state an aggregate hides. */}
                  {result.attempts.map((attempt) => {
                    // The three channels §1.2 Rule 3 asks for — colour,
                    // glyph and word — through the log's own helpers,
                    // against the server's own success list. Not a
                    // second classification of the wire vocabulary.
                    const tone = responseTone(
                      attempt.status,
                      result.success_response_codes,
                    );
                    const glyph = responseGlyph(tone);
                    return (
                      <Text
                        key={attempt.backend_id}
                        size="xs"
                        ff="monospace"
                        className="ddns-data ddns-log__result"
                        data-tone={tone}
                        data-testid={`publishing-attempt-${attempt.backend_type}`}
                      >
                        {glyph ? (
                          <span className="ddns-log__glyph" aria-hidden="true">
                            {glyph}{' '}
                          </span>
                        ) : null}
                        {attempt.backend_type}: {attempt.status}
                        <span className="ddns-sr">
                          {' '}
                          — {responseWord(tone)}
                        </span>
                      </Text>
                    );
                  })}
                  {result.attempts.length === 0 ? (
                    <Text size="xs" c="dimmed">
                      No provider was contacted — this name publishes to
                      nothing.
                    </Text>
                  ) : null}
                  <Text size="xs" c="dimmed">
                    Rate limit: {result.rate_limit_per_minute} per minute for
                    this device.
                  </Text>
                </Stack>
              </Alert>
            ) : null}
          </>
        ) : null}
      </Stack>
    </Modal>
  );
}
