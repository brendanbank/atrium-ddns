/** The board's two on-demand actions — #75, ui-parity §3.3 G3.
 *
 * The legacy `POST /admin/health-checks/run` and `/clear`, which were
 * both `405` on this stack. The gap is a *wait* rather than a missing
 * capability: the check is scheduled, so an operator who has just fixed
 * a provider outage watches a stale board for up to
 * `health_check_interval_minutes` — the board's own payload carries that
 * number, and it is rendered here rather than assumed, so the sentence
 * explaining why the button exists cannot drift from the configuration.
 *
 * ## Four outcomes, kept four
 *
 * The page this sits on already keeps *refused*, *loading*, *failed* and
 * *empty* apart. The same discipline applies to a run's result, and the
 * states are different ones:
 *
 * | state | what it means | what it must not be confused with |
 * |---|---|---|
 * | debounced (429) | pressed again too soon | the run failing |
 * | disabled | `health_check_enabled` is off | a run that found nothing |
 * | ran, 0 checked | nothing in reach has ever published | a clean sweep |
 * | ran, N checked | a measurement | any of the above |
 *
 * `429` in particular is deliberately **not** rendered as an error. The
 * server refused to do work it had already done a moment ago; telling
 * an operator "that did not work" would send them looking for a fault
 * in their DNS.
 *
 * ## Nothing here recomputes a verdict
 *
 * The counts arrive decided (`ok`, `mismatch`, `missing`, `error`,
 * `transitions`) and their consistency is asserted server-side before
 * the response is built. This component formats them and adds up
 * nothing — `ui-design.md` §4.2's prohibition, applied to a summary
 * instead of to a strip.
 */
import { useState } from 'react';
import { Alert, Button, Group, Text } from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  BOARD_QUERY_KEY,
  clearHealthChecks,
  runHealthChecks,
  type HealthCheckClear,
  type HealthCheckRun,
} from '../api/board';
import { ApiError } from '../api/http';

/** What the last press produced. Four constructors, because collapsing
 *  them into `{ message: string }` is how a refusal becomes an error and
 *  a measured zero becomes a silence. */
type Outcome =
  | { kind: 'ran'; summary: HealthCheckRun }
  | { kind: 'cleared'; summary: HealthCheckClear }
  | { kind: 'debounced'; detail: string }
  | { kind: 'failed'; detail: string };

/** The server's `detail`, or the raw body when it is not the shape we
 *  expect. Never a re-worded version: *redact secrets, never
 *  diagnostics*, and the refusal texts here carry the counts an operator
 *  needs. */
function detailOf(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
    } catch {
      /* not JSON — fall through to the body, which is still the
         server's own words rather than ours */
    }
    return error.body || error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

function summarise(summary: HealthCheckRun): string {
  if (!summary.enabled) {
    // Not "0 names checked". The operator turned the check off; saying
    // nothing was found would be a measurement nobody took.
    return 'health checks are switched off — nothing was resolved';
  }
  if (summary.hostnames_checked === 0) {
    // A measured zero, with its denominator beside it.
    return (
      `nothing to check: 0 of ${summary.hostnames_considered} name` +
      `${summary.hostnames_considered === 1 ? '' : 's'} has published an ` +
      `address (${summary.hostnames_never_written} never have)`
    );
  }
  const parts = [
    `${summary.hostnames_checked} of ${summary.hostnames_considered} name` +
      `${summary.hostnames_considered === 1 ? '' : 's'} checked`,
    `${summary.records_checked} record${summary.records_checked === 1 ? '' : 's'}`,
    `${summary.ok} ok`,
    `${summary.mismatch} mismatch`,
    `${summary.missing} missing`,
    `${summary.error} error`,
    `${summary.transitions} changed`,
  ];
  if (summary.hostnames_never_written > 0) {
    parts.push(`${summary.hostnames_never_written} never published`);
  }
  if (summary.truncated) {
    // The ceiling, said out loud. A summary that cannot say it reads
    // identically whether it swept everything or one percent.
    parts.push(`stopped at the ${summary.batch_size}-name batch — more is due`);
  }
  return parts.join(' · ');
}

export function HealthCheckActions({
  intervalMinutes,
}: {
  /** `health_check_interval_minutes`, from the board payload. The wait
   *  this button removes, quoted from the configuration rather than
   *  from a comment. */
  intervalMinutes: number;
}) {
  const client = useQueryClient();
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  const refresh = () => client.invalidateQueries({ queryKey: BOARD_QUERY_KEY });

  const run = useMutation({
    mutationFn: runHealthChecks,
    onSuccess: (summary) => {
      setOutcome({ kind: 'ran', summary });
      void refresh();
    },
    onError: (error: unknown) => {
      // 429 is the debounce, and it is not a failure. The status is a
      // number on `ApiError` precisely so this branch is not a string
      // match on a message the server is free to reword.
      setOutcome(
        error instanceof ApiError && error.status === 429
          ? { kind: 'debounced', detail: detailOf(error) }
          : { kind: 'failed', detail: detailOf(error) },
      );
    },
  });

  const clear = useMutation({
    mutationFn: clearHealthChecks,
    onSuccess: (summary) => {
      setOutcome({ kind: 'cleared', summary });
      void refresh();
    },
    onError: (error: unknown) =>
      setOutcome({ kind: 'failed', detail: detailOf(error) }),
  });

  const busy = run.isPending || clear.isPending;

  return (
    <>
      <Group gap="sm" align="center" data-testid="health-check-actions">
        <Button
          size="xs"
          variant="default"
          disabled={busy}
          onClick={() => run.mutate()}
          data-testid="health-check-run"
        >
          {run.isPending ? 'Checking…' : 'Check now'}
        </Button>
        <Button
          size="xs"
          variant="default"
          disabled={busy}
          onClick={() => clear.mutate()}
          data-testid="health-check-clear"
        >
          Clear results
        </Button>
        <Text size="xs" c="dimmed" data-testid="health-check-cadence">
          {/* Why the button exists, in the configuration's own number. */}
          Otherwise checked every {intervalMinutes} minute
          {intervalMinutes === 1 ? '' : 's'}.
        </Text>
      </Group>

      {outcome === null ? null : outcome.kind === 'ran' ? (
        <Alert
          color="gray"
          variant="light"
          title="Checked"
          data-testid="health-check-ran"
        >
          <Text size="sm" ff="monospace">
            {summarise(outcome.summary)}
          </Text>
        </Alert>
      ) : outcome.kind === 'cleared' ? (
        <Alert
          color="gray"
          variant="light"
          title="Results cleared"
          data-testid="health-check-cleared"
        >
          <Text size="sm" ff="monospace">
            {/* Numerator and denominator in one sentence. `0 of 4` is a
                measurement; `0` on its own is a silence. */}
            {outcome.summary.cleared} of {outcome.summary.in_scope} name
            {outcome.summary.in_scope === 1 ? '' : 's'} reset to “never
            checked”. Nothing was deleted — the log is untouched.
          </Text>
        </Alert>
      ) : outcome.kind === 'debounced' ? (
        <Alert
          color="gray"
          variant="light"
          title="Already just checked"
          data-testid="health-check-debounced"
        >
          <Text size="sm" ff="monospace">
            {outcome.detail}
          </Text>
        </Alert>
      ) : (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="health-check-error"
        >
          {/* Diagnostics in full: the server's own words. */}
          <Text size="sm" ff="monospace">
            {outcome.detail}
          </Text>
        </Alert>
      )}
    </>
  );
}
