/** The empty panel, which is three panels.
 *
 * The acceptance criterion is *"empty state is an invitation to act,
 * not a blank panel"*, and the only way to write an invitation is to
 * know which nothing this is. Rendering *never logged*, *filtered out*
 * and *cannot match* as one blank panel tells a user with a working
 * filter that they have no devices — a claim about their account that
 * is not true.
 *
 * The three arrive as data, not as inference:
 *
 * | `any_rows_in_scope` | `unmatchable_filters` | what it means | the invitation |
 * |---|---|---|---|
 * | `false` | — | nothing has ever been logged for you | add a device |
 * | `true` | `[]` | rows exist, these filters exclude them all | widen or clear |
 * | `true` | non-empty | a filter value nothing can carry | fix that value |
 *
 * `null` never reaches here: it means *not measured*, which the server
 * returns only when the page had rows, and a page with rows is not
 * empty. That is asserted rather than assumed — see the `null` branch.
 *
 * ## What this component deliberately does *not* say
 *
 * There is a fourth nothing and it is not one of these three:
 * *rows exist, they are about you, and they belong to no account so no
 * scope can reach them*. Before #64 that was every `badauth` line and
 * it arrived here as an `unmatchable_filters` entry — a filter reported
 * as structurally unmatchable. It is not that any more: `badauth` rows
 * are now attributed to the owner of the username presented, so the
 * filter matches, and describing it as unmatchable would be an
 * assertion on the report rather than on the thing reported.
 *
 * The residue — an attempt against a username no device holds — is
 * *counted*, not described, and it renders beside this panel as
 * `LogUnattributable`. Keeping it out of here is deliberate: this
 * component answers "which nothing is this", and that count is a
 * measurement that applies just as much when the page is full.
 */
import { Alert, Button, Stack, Text } from '@mantine/core';

import type { EventPage } from '../api/events';

export interface LogEmptyProps {
  page: EventPage;
  activeFilters: number;
  onClear: () => void;
}

export function LogEmpty({ page, activeFilters, onClear }: LogEmptyProps) {
  if (page.any_rows_in_scope === null) {
    // Structurally unreachable: the server measures this only on an
    // empty page. Rendered as a refusal rather than as one of the three
    // real states, because a fourth state quietly rendered as a third
    // is precisely how three states become two.
    return (
      <Alert color="gray" variant="light" data-testid="log-empty-unmeasured">
        <Text size="sm">
          The server did not say whether this account has any log lines.
          That is a bug rather than an empty log — it is measured only
          when a page comes back empty, and this page did.
        </Text>
      </Alert>
    );
  }

  if (page.any_rows_in_scope === false) {
    return (
      <Stack gap="xs" data-testid="log-empty-never">
        <Text>
          Nothing has been logged yet. Your devices write a line here
          every time they call — an update, a delete, or a failed sign-in.
        </Text>
        <Text size="sm" c="dimmed">
          Add a device to get a DDNS username and password, point your
          router at it, and its first call will appear here.
        </Text>
      </Stack>
    );
  }

  if (page.unmatchable_filters.length > 0) {
    return (
      <Stack gap="xs" data-testid="log-empty-unmatchable">
        <Text>
          No lines match, and this zero is not a measurement — one of
          these filters could not have matched:
        </Text>
        {/* Diagnostics in full, with the reason beside the value. The
            value is the reader's own input, so they can see which of
            several it was; the reason is why, because "no rows" has
            three causes on this surface and only one of them means
            nothing happened. */}
        <Stack gap={4} data-testid="log-unmatchable-values">
          {page.unmatchable_filters.map((entry) => (
            <Text key={entry.filter} size="sm">
              <Text span ff="monospace">
                {entry.filter}
              </Text>{' '}
              — {entry.reason}
            </Text>
          ))}
        </Stack>
        <Text size="sm" c="dimmed">
          The query still ran against the last {page.retention_days} days.
        </Text>
        <Button
          variant="light"
          size="xs"
          onClick={onClear}
          data-testid="log-clear-filters"
        >
          Clear {activeFilters} filter{activeFilters === 1 ? '' : 's'}
        </Button>
      </Stack>
    );
  }

  return (
    <Stack gap="xs" data-testid="log-empty-filtered">
      <Text>
        No lines match these filters. There are lines in your log — these
        filters exclude all of them.
      </Text>
      <Text size="sm" c="dimmed">
        The log holds the last {page.retention_days} days. Try widening
        the time range, or clear a filter.
      </Text>
      <Button
        variant="light"
        size="xs"
        onClick={onClear}
        data-testid="log-clear-filters"
      >
        Clear {activeFilters} filter{activeFilters === 1 ? '' : 's'}
      </Button>
    </Stack>
  );
}
