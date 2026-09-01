/** The route atrium registers at `/atrium-ddns/logs` — the log search.
 *
 * Four states, and keeping them four is the point of the component:
 *
 * | state | what it means | what it must not be confused with |
 * |---|---|---|
 * | loading | the request is in flight | an empty log |
 * | failed | the request errored | an empty log |
 * | empty | genuinely nothing to show | any of the above — and it is *itself* three states, see `LogEmpty` |
 * | rows | the normal case | — |
 *
 * ## There is no refusal branch here, and that is the design
 *
 * The device board has one, because the board is gated on
 * `atrium_ddns.device.manage` and a caller without it must be told
 * *"you may not read this"* rather than *"you have no devices"*. This
 * surface has no such gate: **any authenticated caller may read their
 * own log**, because it is their own data. `atrium_ddns.events.read.all`
 * widens the query to every tenant and does nothing else — #14 asserted
 * those are different reaches, and a client-side `usePerm` check here
 * would be the frontend half of the same collapse the server refuses to
 * make.
 *
 * The one permission-shaped decision on this page — whether the user
 * column and the user filter mean anything — arrives as
 * `page.cross_tenant` from the server. Re-deriving it from
 * `usePerm('atrium_ddns.events.read.all')` would be a second
 * implementation of a rule the query already applied, and the two would
 * disagree for a PAT, whose effective permissions are the intersection
 * of its scopes with its owner's.
 */
import { Alert, Anchor, Button, Group, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { activeFilterCount, eventsQuery, type LogQuery } from './api/events';
import { DdnsRoot } from './host/DdnsRoot';
import { LogEmpty } from './logs/LogEmpty';
import { LogFilters } from './logs/LogFilters';
import type { EventRow } from './api/events';
import { LogDetail } from './logs/LogDetail';
import { LogLedger, LogSkeleton } from './logs/LogLedger';
import { LogUnattributable } from './logs/LogUnattributable';
import { devicesQuery } from './api/devices';
import { hostnamesQuery } from './api/hostnames';
import { domainsQuery } from './api/domains';
import { useLogQuery } from './logs/useLogQuery';

export function LogSearchInner() {
  /** The row whose full detail is open, if any. Local state and not
   *  the URL: a log row is a transient thing you glance at, unlike a
   *  zone or a device, and nobody pastes one into a ticket. */
  const [detail, setDetail] = useState<EventRow | null>(null);
  const { query, setFilter, clear } = useLogQuery();
  // Paging state is *not* in the URL. A cursor is a position in one
  // ordering of one filter set — paste it into a ticket a day later and
  // it points into the middle of a different result. The filters are
  // the shareable thing; the page you happened to be on is not.
  const [cursor, setCursor] = useState<string | null>(null);
  const { data, isLoading, error } = useQuery(eventsQuery(query, cursor));

  /** The named things the id filters are about. Any authenticated caller
   *  may read their own log, so these are fetched unconditionally — but a
   *  failure is not fatal: `?? []` leaves the selects empty and every
   *  other filter working, because a log you cannot filter by name is far
   *  better than a log that will not render. */
  const devices = useQuery(devicesQuery({ enabled: true }));
  const hostnames = useQuery(hostnamesQuery({ enabled: true }));
  const domains = useQuery(domainsQuery({ enabled: true }));

  const active = activeFilterCount(query);

  const onFilter = (key: keyof LogQuery, value: string) => {
    // A filter change invalidates the cursor: it is a position in an
    // ordering that no longer exists. Carrying it forward would page
    // into the middle of the new result and call it page one.
    setCursor(null);
    setFilter(key, value);
  };

  return (
    <Stack gap="md">
      <Title order={3}>Log search</Title>

      {data ? (
        <LogFilters
          query={query}
          vocabulary={data.vocabulary}
          applied={data.filters}
          crossTenant={data.cross_tenant}
          devices={devices.data ?? []}
          hostnames={hostnames.data ?? []}
          domains={domains.data ?? []}
          /* Derived from the rows on screen, deduped. This bundle owns no
             user directory — atrium does — and the rows already carry
             `user_email` beside `user_id`, which is what the ledger
             renders. Exact for the chip (a result filtered to one user
             carries that user on every row); necessarily partial for the
             dropdown, which its placeholder says. */
          users={Array.from(
            new Map(
              (data.rows ?? [])
                .filter((row) => row.user_id !== null && row.user_email)
                .map((row) => [
                  row.user_id as number,
                  { id: row.user_id as number, email: row.user_email as string },
                ]),
            ).values(),
          )}
          onChange={onFilter}
          onClear={() => {
            setCursor(null);
            clear();
          }}
        />
      ) : null}

      {isLoading ? (
        <LogSkeleton />
      ) : error ? (
        <Alert
          color="gray"
          variant="light"
          title="Could not load the log"
          data-testid="log-error"
        >
          {/* Diagnostics in full: the status and the server's own
              words. Redact secrets, never diagnostics. */}
          <Text size="sm" ff="monospace">
            {(error as Error).message}
          </Text>
        </Alert>
      ) : data ? (
        data.rows.length === 0 ? (
          <>
            <LogEmpty
              page={data}
              activeFilters={active}
              onClear={() => {
                setCursor(null);
                clear();
              }}
            />
            {/* #64. It renders on *both* branches, and that is the
                point rather than a duplication: a tenant with two
                attributed failures needs to know there are forty they
                cannot see just as much as a tenant with none does. On
                the empty branch it is what stops the zero reading as
                "my credentials are fine"; on the ledger branch it is
                what stops a short list reading as the whole story. */}
            <LogUnattributable
              tally={data.unattributable}
              crossTenant={data.cross_tenant}
            />
          </>
        ) : (
          <>
            <LogUnattributable
              tally={data.unattributable}
              crossTenant={data.cross_tenant}
            />
            <LogLedger page={data} onOpen={setDetail}
          onFilter={onFilter} />
            <Group gap="sm" align="center">
              {cursor !== null ? (
                <Button
                  variant="default"
                  size="xs"
                  onClick={() => setCursor(null)}
                  data-testid="log-first-page"
                >
                  Back to newest
                </Button>
              ) : null}
              {data.next_cursor !== null ? (
                <Button
                  variant="default"
                  size="xs"
                  onClick={() => setCursor(data.next_cursor)}
                  data-testid="log-next-page"
                >
                  Older
                </Button>
              ) : (
                /* A measured end, not a missing button. "That is
                   everything" and "the control failed to render" look
                   identical when the only signal is an absence. */
                <span className="ddns-label" data-testid="log-end">
                  end of the log for these filters
                </span>
              )}
              <Text size="xs" c="dimmed" data-testid="log-window">
                showing up to {data.limit} lines · the log holds the last{' '}
                {data.retention_days} days
              </Text>
            </Group>
          </>
        )
      ) : null}

      <LogDetail row={detail} onClose={() => setDetail(null)} />
    </Stack>
  );
}

export function LogSearchPage() {
  return (
    <DdnsRoot>
      <LogSearchInner />
    </DdnsRoot>
  );
}

/** Where a pre-applied link points. Exported so the board's links and
 *  the registered route are one string rather than two that drift. */
export const LOG_PATH = '/atrium-ddns/logs';

/** A link to this surface with a filter already applied.
 *
 * The issue's phrase is *"reachable pre-applied from any device or
 * hostname row"*, and this is what makes that true. Built from
 * `URLSearchParams` rather than by string concatenation so a hostname
 * containing a character that needs escaping cannot produce a URL that
 * silently filters on something else.
 */
export function logHref(params: Record<string, string | number>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    search.set(key, String(value));
  }
  const suffix = search.toString();
  return `${LOG_PATH}${suffix ? `?${suffix}` : ''}`;
}

/** The board's "see this device's log" affordance.
 *
 * A plain anchor, deliberately. The host bundle's React tree is mounted
 * inside atrium's (`makeWrapperElement`), so react-router's `Link` is
 * not reachable from here — and a `history.pushState` from outside
 * atrium's router would move the address bar without telling the router,
 * leaving the shell rendering the previous route. An anchor hands the
 * navigation back to the browser, which is the one participant that
 * cannot get it wrong.
 */
export function LogLink({
  children,
  params,
  ...rest
}: {
  children: React.ReactNode;
  params: Record<string, string | number>;
  'data-testid'?: string;
}) {
  return (
    <Anchor href={logHref(params)} size="xs" {...rest}>
      {children}
    </Anchor>
  );
}
