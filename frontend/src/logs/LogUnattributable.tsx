/** The count that stops a zero being read as reassurance.
 *
 * #64, second half. The first half changed the writer: an
 * authentication failure whose username resolves to a device is now
 * attributed to that device's owner, so a tenant filtering the log for
 * `badauth` sees their own failures instead of an unconditional zero.
 *
 * That leaves the half nobody owns. An attempt whose username matches
 * **no** device has no owner to attribute it to — and that is precisely
 * the shape a router configured with a mistyped username produces. So a
 * tenant's zero on this filter still does not mean *no failures*; it
 * means *no failures against a username that belongs to me*. Those are
 * two different facts and the acceptance criterion says they must not
 * render identically.
 *
 * They do not, because this is a **measurement and not a caption**.
 * Asked what it would print if the thing it measures were absent:
 *
 * | server sent | what renders |
 * |---|---|
 * | `null` (not asked — the filter is not on `badauth`) | nothing at all |
 * | `{ rows: 0 }` | "none in this window" — a measured zero |
 * | `{ rows: 41 }` | "41 … could not be attributed to any account" |
 *
 * Three strings for three states. A caption that said "some failures
 * may not be attributable" would print the same words in all three, and
 * would therefore be worth nothing — which is what the version of this
 * surface before #64 shipped.
 */
import { Alert, List, Stack, Text } from '@mantine/core';

import type { UnattributableTally } from '../api/events';

export interface LogUnattributableProps {
  tally: UnattributableTally | null;
  /** Whether this caller reads across tenants. A cross-tenant reader
   *  sees the ownerless rows in the ledger itself, so telling them the
   *  count separately is restating what is already on screen — and
   *  worse, implies the rows are hidden from them. */
  crossTenant: boolean;
}

/** Human date, in the browser's own locale. Not `toISOString()`: this
 *  string is read, not parsed, and the ledger renders timestamps the
 *  same way. */
function when(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

export function LogUnattributable({
  tally,
  crossTenant,
}: LogUnattributableProps) {
  if (tally === null || crossTenant) return null;

  const measuredZero = tally.rows === 0;

  return (
    <Alert
      color={measuredZero ? 'gray' : 'yellow'}
      variant="light"
      data-testid="log-unattributable"
      data-rows={tally.rows}
    >
      <Stack gap={4}>
        <Text size="sm">
          {measuredZero ? (
            <>
              <Text span fw={600} data-testid="log-unattributable-count">
                No
              </Text>{' '}
              <Text span ff="monospace">
                {tally.response_code}
              </Text>{' '}
              lines in this window could not be attributed to an account.
              So a zero above is a zero: nothing was refused against a
              username this service does not issue either.
            </>
          ) : (
            <>
              <Text span fw={600} data-testid="log-unattributable-count">
                {tally.rows}
              </Text>{' '}
              <Text span ff="monospace">
                {tally.response_code}
              </Text>{' '}
              {tally.rows === 1 ? 'line' : 'lines'} in this window could
              not be attributed to any account, and are not shown above —
              yours or anyone&apos;s. A sign-in is attributed to the owner
              of the username it presents, and these presented a username
              this service has never issued.{' '}
              <Text span fw={600}>
                If your router is configured with a mistyped username, its
                failures are in that number and not in the list above.
              </Text>
            </>
          )}
        </Text>
        {/* The population, printed beside the figure. A count with no
            divisor is the thing this component exists to replace. */}
        <Text size="xs" c="dimmed" data-testid="log-unattributable-window">
          counted from {when(tally.since)}
          {tally.until === null ? ' to now' : ` to ${when(tally.until)}`}
        </Text>
        {tally.ignored_filters.length > 0 ? (
          <>
            <Text size="xs" c="dimmed">
              These filters could not narrow that count — a line with no
              account carries none of them, so applying them would have
              returned zero whatever the truth was:
            </Text>
            <List size="xs" c="dimmed" data-testid="log-unattributable-ignored">
              {tally.ignored_filters.map((entry) => (
                <List.Item key={entry}>
                  <Text span ff="monospace" size="xs">
                    {entry}
                  </Text>
                </List.Item>
              ))}
            </List>
          </>
        ) : null}
        {measuredZero ? null : (
          /* The admin path the acceptance criterion asks for, named
             rather than linked. `atrium_ddns.events.read.all` widens
             this same query to every row including these — it is the
             existing grant, not a new surface — and there is no route
             a tenant could be sent to, because the whole point is that
             they may not read them. */
          <Text size="xs" c="dimmed" data-testid="log-unattributable-admin">
            An operator holding <Text span ff="monospace" size="xs">
              atrium_ddns.events.read.all
            </Text>{' '}
            sees these lines in full, on this same screen. Ask one to look
            if your router is failing and nothing above explains it.
          </Text>
        )}
      </Stack>
    </Alert>
  );
}
