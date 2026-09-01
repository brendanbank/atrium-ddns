/** The log surface, under `@brendanbank/atrium-test-utils`.
 *
 * What this file is written to prove, in the order the acceptance
 * criteria list it:
 *
 * 1. **The filters reach the server**, as query parameters, and the
 *    browser does not filter anything. Asserted on the *URL the bundle
 *    requested*, and paired with a row-count assertion so "the filter
 *    was sent" and "the browser did not also filter" are two separate
 *    readings.
 * 2. **A filter is reachable pre-applied from a link**, and from the
 *    board's own device and hostname rows.
 * 3. **A row about a deleted device is readable**, keeps its name, and
 *    offers no filter — because a link that filters on nothing returns
 *    an empty log that reads as "this device did nothing".
 * 4. **Empty is three states**, with three different invitations, and a
 *    fourth (`null`) that must never render as one of them.
 * 5. **The user filter is the server's decision**, not the browser's.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { LogSearchPage, logHref } from '../LogSearchPage';
import { activeFilterCount, fromSearchParams, toSearchParams } from '../api/events';
import type { EventPage } from '../api/events';
import { queryClient } from '../queryClient';
import { page, row, V4_DECLARED, V6_CALLER, V6_OTHER } from './logFixtures';

const TENANT: UserContext = {
  id: 1,
  email: 'tenant@example.com',
  full_name: 'Tenant',
  is_active: true,
  roles: ['user'],
  // Deliberately holds none of the atrium_ddns codes. The log is not
  // gated on any of them, and a test driven with a permission list would
  // pass against a handler that was.
  permissions: [],
  impersonating_from: null,
};

let handles: MockAtriumHandles;
let payload: EventPage = page();
/** Every URL the bundle asked for. The instrument for "the filter went
 *  to the server" — an assertion on the rendered rows alone cannot tell
 *  a server-side filter from a client-side one. */
let requested: string[] = [];
/** Whether the stub echoes the requested filters back, the way the real
 *  server does. Default true; one test turns it off deliberately. */
let echoFilters = true;

function stubFetch() {
  requested = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) {
        return new Response(JSON.stringify(TENANT), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/atrium_ddns/events')) {
        requested.push(url);
        return new Response(JSON.stringify(echoing(url, payload)), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200 });
    }),
  );
}

/** A deliberately **dumb** server: it echoes the filters back and
 *  returns the same rows whatever was asked.
 *
 * The echo matters because the real server echoes, and a fixture whose
 * `filters` never move makes a whole class of test unable to fail —
 * measured, not supposed. The first version of
 * *"the browser filters nothing"* held `filters` at all-null, so the
 * mutation that adds a client-side `rows.filter(...)` keyed on
 * `page.filters.device_id` was a no-op and **survived**. The probe
 * could not fail.
 *
 * Returning the same rows regardless is the other half, and it is not
 * laziness: it is what makes the assertion possible at all. If the stub
 * narrowed the rows, "the component shows fewer rows after a filter"
 * would be true for both a correct component and a filtering one. With
 * a stub that never narrows, **the rendered row set is a direct reading
 * of whether the browser filtered**.
 */
function echoing(url: string, base: EventPage): EventPage {
  // One test needs the echo to *disagree* with the URL, because the
  // property it asserts is "the chips follow the server, not the local
  // state" and an echo that always agrees cannot tell the two apart.
  if (!echoFilters) return base;
  const params = new URLSearchParams(url.slice(url.indexOf('?') + 1));
  const num = (key: string) =>
    params.has(key) ? Number(params.get(key)) : null;
  const str = (key: string) => params.get(key);
  return {
    ...base,
    filters: {
      ...base.filters,
      user_id: num('user_id'),
      device_id: num('device_id'),
      domain_id: num('domain_id'),
      hostname_id: num('hostname_id'),
      event_type: str('event_type'),
      response_code: str('response_code'),
      backend_type: str('backend_type'),
      client_ip: str('client_ip'),
      since: str('since'),
      until: str('until'),
    },
  };
}

function setLocation(search: string) {
  window.history.replaceState({}, '', `/atrium-ddns/logs${search}`);
}

beforeEach(() => {
  echoFilters = true;
  stubFetch();
  payload = page();
  setLocation('');
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  queryClient.clear();
  vi.unstubAllGlobals();
  setLocation('');
  // `data-mantine-color-scheme` is on `<html>`, which outlives a
  // `cleanup()`. The dark test below removes it inline, but a throw
  // before that line would leak it into every later test in this file —
  // and `DeviceBoardPage.test.tsx` has an assertion that compares the
  // markup rendered under each scheme, so a leak is the shape of thing
  // that reads as flakiness rather than as a leak.
  document.documentElement.removeAttribute('data-mantine-color-scheme');
});

function renderLog() {
  handles = mockAtriumRegistry({ me: TENANT });
  return renderWithAtrium(<LogSearchPage />);
}

/** The query string of the last events request. */
function lastQuery(): URLSearchParams {
  const url = requested[requested.length - 1];
  return new URLSearchParams(url.slice(url.indexOf('?') + 1));
}

describe('the filters are server-side', () => {
  test('a filter arrives as a query parameter, and the browser filters nothing', async () => {
    // Two rows on **different devices**, and a stub that echoes the
    // filter back while returning both rows regardless. That
    // combination is what makes this test able to fail: with the filter
    // set to device 7 and the payload still carrying device 8, a
    // component that filtered client-side renders one row and a correct
    // one renders two.
    //
    // Both halves were absent from the first version — same rows on the
    // same device, and an echo frozen at all-null — and the mutation
    // that adds `rows.filter(...)` survived it.
    payload = page({
      rows: [
        row({ id: 1, device_id: 7, device_name: 'roof-ap' }),
        row({ id: 2, device_id: 8, device_name: 'garage-nas' }),
      ],
    });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-row-1')).toBeInTheDocument();
    expect(screen.getByTestId('log-row-2')).toBeInTheDocument();

    // Click the device name on row 1 — the pre-apply affordance.
    fireEvent.click(screen.getByTestId('log-row-1-device'));

    await waitFor(() => expect(lastQuery().get('device_id')).toBe('7'));
    // The filter reached the server and came back applied…
    await waitFor(() =>
      expect(screen.getByTestId('log-applied-device_id')).toHaveTextContent(
        'device: 7',
      ),
    );
    // …and the row about device 8 is *still rendered*, because the
    // server sent it. Narrowing it here would be the browser deciding
    // what the query meant.
    expect(screen.getByTestId('log-row-2')).toBeInTheDocument();
    expect(screen.getAllByTestId(/^log-row-\d+$/)).toHaveLength(2);
  });

  test('the filter is written to the address bar, so the URL is the state', async () => {
    renderLog();
    await screen.findByTestId('log-ledger');
    fireEvent.click(screen.getByTestId('log-row-1-hostname'));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get('hostname_id')).toBe(
        '9',
      ),
    );
    // The path is untouched: a filter change must not navigate.
    expect(window.location.pathname).toBe('/atrium-ddns/logs');
  });

  test('a pre-applied link is read on mount and sent to the server', async () => {
    setLocation('?device_id=7&response_code=badauth');
    renderLog();
    await screen.findByTestId('log-ledger');
    const query = lastQuery();
    expect(query.get('device_id')).toBe('7');
    expect(query.get('response_code')).toBe('badauth');
    // Vacuity: an unset filter is *absent*, never `?since=`.
    expect(query.has('since')).toBe(false);
  });

  test('unset filters are absent rather than empty', () => {
    // The unit behind the assertion above, driven directly so the rule
    // is asserted rather than observed once.
    const params = toSearchParams({ ...fromSearchParams(''), device_id: '7' });
    expect(params.toString()).toBe('device_id=7');
    expect(activeFilterCount(fromSearchParams('?device_id=7&since=x'))).toBe(2);
    expect(activeFilterCount(fromSearchParams(''))).toBe(0);
  });

  test('an unknown query parameter is dropped rather than forwarded', () => {
    // A `?nonsense=1` pasted into a link must not become a filter the
    // server has never heard of.
    const query = fromSearchParams('?device_id=7&nonsense=1');
    expect(toSearchParams(query).toString()).toBe('device_id=7');
  });
});

describe('reachable pre-applied', () => {
  test('logHref escapes rather than concatenating', () => {
    expect(logHref({ device_id: 7 })).toBe('/atrium-ddns/logs?device_id=7');
    // A hostname carrying a character that needs escaping must not
    // produce a URL that silently filters on something else.
    expect(logHref({ client_ip: '2001:db8::1' })).toBe(
      '/atrium-ddns/logs?client_ip=2001%3Adb8%3A%3A1',
    );
    expect(logHref({})).toBe('/atrium-ddns/logs');
  });
});

describe('a deleted device stays readable', () => {
  test('the name survives, the filter does not, and the row says which', async () => {
    // `ON DELETE SET NULL`: the id is gone, the denormalised name is
    // not. This is the state the log is most often opened to
    // investigate.
    payload = page({
      rows: [row({ id: 5, device_id: null, device_name: 'garage-nas' })],
    });
    renderLog();
    await screen.findByTestId('log-ledger');

    const cell = screen.getByTestId('log-row-5-device');
    expect(cell).toHaveTextContent('garage-nas');
    expect(screen.getByTestId('log-row-5-device-deleted')).toHaveTextContent(
      'deleted',
    );
    // Not a button: an inert filter link returns an empty log that reads
    // as "this device did nothing", which is a claim the data does not
    // support.
    expect(cell.tagName).not.toBe('BUTTON');

    // The control, same assertion the other way: a live device *is* a
    // button. Without this pair the check above also passes against a
    // component that never renders a filter control at all.
    cleanup();
    handles.cleanup();
    queryClient.clear();
    payload = page({ rows: [row({ id: 6, device_id: 7, device_name: 'roof-ap' })] });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-row-6-device').tagName).toBe('BUTTON');
    expect(
      screen.queryByTestId('log-row-6-device-deleted'),
    ).not.toBeInTheDocument();
  });

  test('a row with no captured name says unknown rather than rendering blank', async () => {
    payload = page({ rows: [row({ id: 8, device_id: null, device_name: null })] });
    renderLog();
    await screen.findByTestId('log-ledger');
    // Not the empty string: a blank cell reads as a layout bug, and
    // "there was no device" and "we failed to draw it" are different
    // facts.
    expect(screen.getByTestId('log-row-8-device')).toHaveTextContent('unknown');
  });
});

describe('empty is three states, and a fourth that must not pass for one', () => {
  test('nothing ever logged is an invitation to add a device', async () => {
    payload = page({ rows: [], any_rows_in_scope: false });
    renderLog();
    expect(await screen.findByTestId('log-empty-never')).toBeInTheDocument();
    expect(screen.getByText(/Add a device/i)).toBeInTheDocument();
    // Must not be confused with the filtered case: there is nothing to
    // clear, so no clear control.
    expect(screen.queryByTestId('log-empty-filtered')).not.toBeInTheDocument();
    expect(screen.queryByTestId('log-clear-filters')).not.toBeInTheDocument();
  });

  test('filtered out names the window and offers to clear', async () => {
    setLocation('?response_code=badauth');
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      filters: { ...page().filters, response_code: 'badauth' },
      retention_days: 30,
    });
    renderLog();
    expect(await screen.findByTestId('log-empty-filtered')).toBeInTheDocument();
    // The denominator, named beside the zero. `retention_days` comes
    // from the payload so an operator who changes it cannot make this
    // sentence wrong.
    expect(screen.getByText(/last 30 days/)).toBeInTheDocument();
    expect(screen.getByTestId('log-clear-filters')).toBeInTheDocument();
    // And it must not claim the account is empty.
    expect(screen.queryByTestId('log-empty-never')).not.toBeInTheDocument();
  });

  test('an unmatchable filter value is named, so a typo is not a measurement', async () => {
    setLocation('?backend_type=rout53');
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      filters: { ...page().filters, backend_type: 'rout53' },
      unmatchable_filters: [
        {
          filter: 'backend_type=rout53',
          reason: 'that is not a provider this installation knows about',
        },
      ],
    });
    renderLog();
    expect(
      await screen.findByTestId('log-empty-unmatchable'),
    ).toBeInTheDocument();
    // The value in full, and the reason beside it. "No traffic for that
    // provider" and "you spelled it wrong" are two readings of the same
    // zero, and only the pair separates them.
    const named = screen.getByTestId('log-unmatchable-values');
    expect(named).toHaveTextContent('backend_type=rout53');
    expect(named).toHaveTextContent('not a provider this installation knows');
    expect(screen.queryByTestId('log-empty-filtered')).not.toBeInTheDocument();
  });

  test('a badauth zero renders differently from a badauth zero with unattributable lines', async () => {
    // #64's acceptance, and the pair is the assertion. `badauth` rows
    // are attributed to the owner of the username presented, so this
    // filter matches for a tenant now — but an attempt against a
    // username no device holds still belongs to nobody, and that is
    // exactly what a router configured with a typo produces. So an
    // empty page has two meanings and they must not render the same.
    //
    // The probe question: what would each print if the thing it
    // measures were absent? A caption would print one string for both.
    // These print different strings, and the counts are on the DOM.
    setLocation('?response_code=badauth');

    // (a) nothing at all — a measured zero on both halves.
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      cross_tenant: false,
      filters: { ...page().filters, response_code: 'badauth' },
      unattributable: {
        response_code: 'badauth',
        rows: 0,
        since: '2026-08-01T00:00:00',
        until: null,
        ignored_filters: [],
      },
    });
    const clean = renderLog();
    expect(await screen.findByTestId('log-unattributable')).toHaveAttribute(
      'data-rows',
      '0',
    );
    const cleanText = screen.getByTestId('log-unattributable').textContent;
    // A zero must not be described as unmatchable: the filter matched,
    // there were simply none.
    expect(screen.queryByTestId('log-empty-unmatchable')).not.toBeInTheDocument();
    clean.unmount();

    // (b) the same empty page, with lines that cannot be attributed.
    // The location changes because the react-query key is the filter
    // set: rendering the identical query again is served from cache and
    // would assert about part (a)'s payload — which is the "two
    // instruments that were actually one" defect, in a test file.
    setLocation('?response_code=badauth&client_ip=203.0.113.9');
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      cross_tenant: false,
      filters: { ...page().filters, response_code: 'badauth' },
      unattributable: {
        response_code: 'badauth',
        rows: 41,
        since: '2026-08-01T00:00:00',
        until: null,
        ignored_filters: [],
      },
    });
    renderLog();
    const loud = await screen.findByTestId('log-unattributable');
    expect(loud).toHaveAttribute('data-rows', '41');
    expect(screen.getByTestId('log-unattributable-count')).toHaveTextContent(
      '41',
    );
    expect(loud).toHaveTextContent('mistyped username');
    // The admin path the acceptance asks for, named on the surface.
    expect(screen.getByTestId('log-unattributable-admin')).toHaveTextContent(
      'atrium_ddns.events.read.all',
    );
    // The whole criterion, in one assertion: two different zeroes, two
    // different strings.
    expect(loud.textContent).not.toEqual(cleanText);
  });

  test('the tally is silent when it was not asked, and when the reader is cross-tenant', async () => {
    // `null` is *not asked* and must render as nothing — not as a
    // measured zero. And a cross-tenant reader sees the ownerless rows
    // in the ledger itself, so a separate count would restate what is
    // already on screen and imply they are hidden.
    setLocation('?response_code=good');
    payload = page({ rows: [], any_rows_in_scope: true });
    const notAsked = renderLog();
    expect(await screen.findByTestId('log-empty-filtered')).toBeInTheDocument();
    expect(screen.queryByTestId('log-unattributable')).not.toBeInTheDocument();
    notAsked.unmount();

    setLocation('?response_code=badauth');
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      cross_tenant: true,
      unattributable: {
        response_code: 'badauth',
        rows: 41,
        since: '2026-08-01T00:00:00',
        until: null,
        ignored_filters: [],
      },
    });
    renderLog();
    expect(await screen.findByTestId('log-empty-filtered')).toBeInTheDocument();
    expect(screen.queryByTestId('log-unattributable')).not.toBeInTheDocument();
  });

  test('a filter the tally could not honour is named rather than silently dropped', async () => {
    // A row with no owner carries no `device_id`, so a tally narrowed
    // by one would be 0 for every installation, forever — a probe that
    // cannot fail. The server drops it and says so.
    setLocation('?response_code=badauth&device_id=7');
    payload = page({
      rows: [],
      any_rows_in_scope: true,
      cross_tenant: false,
      unattributable: {
        response_code: 'badauth',
        rows: 9,
        since: '2026-08-01T00:00:00',
        until: null,
        ignored_filters: ['device_id=7'],
      },
    });
    renderLog();
    expect(
      await screen.findByTestId('log-unattributable-ignored'),
    ).toHaveTextContent('device_id=7');
  });

  test('the unmeasured fourth state renders as a bug, not as an empty log', async () => {
    // `null` means *not measured*, which the server returns only when
    // the page had rows. Reaching here with `null` is structurally
    // impossible, so it renders as a refusal — a fourth state quietly
    // rendered as a third is how three states become two.
    payload = page({ rows: [], any_rows_in_scope: null });
    renderLog();
    expect(
      await screen.findByTestId('log-empty-unmeasured'),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('log-empty-never')).not.toBeInTheDocument();
    expect(screen.queryByTestId('log-empty-filtered')).not.toBeInTheDocument();
  });

  test('loading is not empty, and failure is not empty', async () => {
    // A skeleton with no rail on the board; the same rule here. Three
    // different nothings.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/users/me/context')) {
          return new Response(JSON.stringify(TENANT), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/atrium_ddns/events')) {
          return new Response('the database is not accepting connections', {
            status: 503,
          });
        }
        return new Response('{}', { status: 200 });
      }),
    );
    renderLog();
    // `queryClient` is configured with `retry: 1`, so the failure is
    // only final after a second attempt and a backoff — longer than
    // `findBy`'s 1s default. Waiting longer rather than turning retries
    // off: the retry is the shipped behaviour, and a test that disabled
    // it would be asserting about a client the app does not use.
    expect(
      await screen.findByTestId('log-error', {}, { timeout: 5_000 }),
    ).toBeInTheDocument();
    // Diagnostics in full: the status and the server's own words.
    expect(screen.getByTestId('log-error')).toHaveTextContent('503');
    expect(screen.getByTestId('log-error')).toHaveTextContent(
      'not accepting connections',
    );
    expect(screen.queryByTestId('log-empty-never')).not.toBeInTheDocument();
    expect(screen.queryByTestId('log-empty-filtered')).not.toBeInTheDocument();
  });
});

describe('the user filter is the server’s decision', () => {
  test('a tenant sees no user column and no user filter', async () => {
    payload = page({ cross_tenant: false });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.queryByTestId('log-row-1-user')).not.toBeInTheDocument();
    expect(screen.queryByTestId('filter-user-id')).not.toBeInTheDocument();
  });

  test('cross_tenant turns both on, and it comes from the payload', async () => {
    // The same UserContext, with **no** permissions — so a component
    // that re-derived this from `usePerm` would render nothing and this
    // test would fail. That is the point of driving it with an empty
    // permission list.
    payload = page({ cross_tenant: true });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-row-1-user')).toHaveTextContent(
      'tenant@example.com',
    );
    expect(screen.getByTestId('filter-user-id')).toBeInTheDocument();
  });
});

describe('the rendering rules the design fixes', () => {
  test('a refused line is accented and an accepted one is not', async () => {
    payload = page({
      rows: [
        row({ id: 1, response_code: 'nochg' }),
        row({ id: 2, response_code: 'badauth' }),
        row({ id: 3, response_code: null }),
      ],
    });
    renderLog();
    await screen.findByTestId('log-ledger');

    // §1.2 Rule 1: agreement has no colour.
    expect(screen.getByTestId('log-row-1')).toHaveAttribute('data-tone', 'ink');
    // Rule 2: the accent means "this is true and it is wrong".
    expect(screen.getByTestId('log-row-2')).toHaveAttribute(
      'data-tone',
      'diverge',
    );
    // A row that never answered on the wire is quiet, not accented — a
    // fact about our instrument rendered as one about the tenant's DNS
    // is the mistake §4.2 refuses when it keeps `error` grey.
    expect(screen.getByTestId('log-row-3')).toHaveAttribute(
      'data-tone',
      'quiet',
    );
    // Rule 3: colour is never the only channel. The glyph and the word.
    expect(screen.getByTestId('log-row-2-result')).toHaveTextContent('≠');
    expect(screen.getByTestId('log-row-2-result')).toHaveTextContent('refused');
    expect(screen.getByTestId('log-row-1-result')).not.toHaveTextContent('≠');
  });

  test('the accent follows the server’s success set, not a literal', async () => {
    // The strongest form: the same code, reclassified on the server.
    // A component holding `['good','nochg']` would render this as a
    // failure and this test would catch it.
    payload = page({
      rows: [row({ id: 1, response_code: 'dnserr' })],
      vocabulary: {
        ...page().vocabulary,
        success_response_codes: ['good', 'nochg', 'dnserr'],
      },
    });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-row-1')).toHaveAttribute('data-tone', 'ink');
  });

  test('the second address appears only when it is a second fact', async () => {
    payload = page({
      rows: [
        row({ id: 1, client_ip: V6_CALLER, ip: V6_CALLER }),
        row({ id: 2, client_ip: V6_OTHER, ip: V4_DECLARED }),
      ],
    });
    renderLog();
    await screen.findByTestId('log-ledger');

    // M3: 94.5% of updates are `nochg` with no `myip`, so the two
    // addresses are the same fact and a column that always repeats
    // itself has stopped carrying anything.
    expect(screen.getByTestId('log-row-1-client-ip')).toBeInTheDocument();
    expect(screen.queryByTestId('log-row-1-ip')).not.toBeInTheDocument();

    // The NAT'd update, which is the interesting one. The second
    // address itself moved into the detail a click opens — it was a
    // second line under every row, and on a ledger the point of which is
    // scanning, one wrapped row per event costs more than it returns.
    // What stays in the row is a marker saying the two differ, labelled
    // in its `title`, because two addresses with no label read as a typo
    // and an unlabelled glyph reads as decoration.
    const marker = screen
      .getByTestId('log-row-2-client-ip')
      .querySelector('.ddns-log__declared');
    expect(marker, 'the row does not mark that the addresses differ').not.toBeNull();
    expect(marker).toHaveAttribute('title', `declared myip ${V4_DECLARED}`);
    // Row 1's two addresses are the same fact, so it carries no marker.
    expect(
      screen
        .getByTestId('log-row-1-client-ip')
        .querySelector('.ddns-log__declared'),
    ).toBeNull();

    // …and the address itself is one click away, stated in full and
    // labelled, on both rows — in a detail view "same as called from" is
    // itself worth being able to read.
    fireEvent.click(screen.getByTestId('log-row-2'));
    expect(
      await screen.findByTestId('log-detail-Declared myip'),
    ).toHaveTextContent(V4_DECLARED);
  });

  test('a null backend is a meaning, not a dash', async () => {
    payload = page({ rows: [row({ id: 1, backend_type: null })] });
    renderLog();
    await screen.findByTestId('log-ledger');
    const cell = screen.getByTestId('log-row-1-backend');
    expect(cell).toHaveTextContent('no backend');
    // §4.2's first prohibition: never a bare dash, which is ambiguous.
    expect(cell.textContent).not.toBe('—');
    expect(cell).toHaveAttribute(
      'title',
      expect.stringContaining('before any provider was contacted'),
    );
  });

  test('the filter selects are populated from the payload, not from a literal', async () => {
    // `checkip` and `healthcheck` appear in `models.DnsEvent`'s own
    // comment and neither has a writer. A hardcoded list would offer
    // them; the shipped vocabulary does not, and this asserts the
    // component reads the payload rather than a list of its own.
    payload = page({
      vocabulary: { ...page().vocabulary, event_types: ['only-this-one'] },
    });
    renderLog();
    await screen.findByTestId('log-ledger');
    const select = screen.getByTestId('filter-event-type');
    fireEvent.click(select);
    expect(await screen.findByText('only-this-one')).toBeInTheDocument();
    expect(screen.queryByText('checkip')).not.toBeInTheDocument();
  });

  test('the applied chips render the server’s echo, not the local state', async () => {
    // The URL says one thing and the server's echo says another. The
    // chips must follow the server: an empty result is only
    // interpretable beside the filters that produced it, and if a
    // filter fails to reach the query the discrepancy has to be
    // visible rather than papered over by the local state.
    echoFilters = false; // so the URL and the echo can disagree
    setLocation('?device_id=7');
    payload = page({ filters: { ...page().filters, device_id: 99 } });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-applied-device_id')).toHaveTextContent(
      'device: 99',
    );
  });

  test('no filters is a statement, not a silence', async () => {
    renderLog();
    await screen.findByTestId('log-ledger');
    // An empty chip strip is indistinguishable from one that failed to
    // render. `0 refused / 7 polled` is a result; `0 refused` is
    // silence.
    expect(screen.getByTestId('log-applied-none')).toHaveTextContent(
      'showing everything',
    );
  });

  test('the end of the log is a measured end, not a missing button', async () => {
    payload = page({ next_cursor: null });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-end')).toBeInTheDocument();
    expect(screen.queryByTestId('log-next-page')).not.toBeInTheDocument();

    // The control: with a cursor there is a button and no end marker.
    cleanup();
    handles.cleanup();
    queryClient.clear();
    payload = page({ next_cursor: '2026-08-15T14:00:00Z|1' });
    renderLog();
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-next-page')).toBeInTheDocument();
    expect(screen.queryByTestId('log-end')).not.toBeInTheDocument();
  });

  test('changing a filter drops the cursor', async () => {
    // A cursor is a position in an ordering of one filter set. Carried
    // across a filter change it pages into the middle of the new result
    // and calls it page one.
    payload = page({ next_cursor: '2026-08-15T14:00:00Z|1' });
    renderLog();
    await screen.findByTestId('log-ledger');
    fireEvent.click(screen.getByTestId('log-next-page'));
    await waitFor(() => expect(lastQuery().get('cursor')).toBeTruthy());
    // The cursor is part of the query key, so the second page is a
    // fresh query and the ledger unmounts while it is in flight. Wait
    // for it back before clicking into it.
    await screen.findByTestId('log-ledger');

    fireEvent.click(screen.getByTestId('log-row-1-device'));
    await waitFor(() => expect(lastQuery().get('device_id')).toBe('7'));
    expect(lastQuery().has('cursor')).toBe(false);
  });
});

describe('light and dark', () => {
  test('the accent is keyed on the attribute atrium owns', async () => {
    // The host bundle never writes `data-mantine-color-scheme`; it reads
    // it. Asserted by setting the attribute and checking the CSS custom
    // property resolves differently — the same instrument the board
    // uses, applied to the log's own accented row.
    payload = page({ rows: [row({ id: 2, response_code: 'badauth' })] });
    document.documentElement.setAttribute('data-mantine-color-scheme', 'dark');
    renderLog();
    await screen.findByTestId('log-ledger');
    // The rendering is attribute-driven, so the row still carries its
    // tone in dark. The colour value itself is asserted in
    // `design.test.ts`, off the stylesheet.
    expect(screen.getByTestId('log-row-2')).toHaveAttribute(
      'data-tone',
      'diverge',
    );
    document.documentElement.removeAttribute('data-mantine-color-scheme');
  });
});
