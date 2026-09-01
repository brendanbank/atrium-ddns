/** The board and the log, for the four hand-reported behaviours that
 *  belong to neither `deviceCard.test.tsx` nor `hostnameSuffix.test.ts`.
 *
 * See `docs/ops/hand-reported-sweep.md` for the whole population and how
 * it was recovered. This file owns four of it:
 *
 * | # | reported | record |
 * |---|---|---|
 * | 10 | *"Add a device" navigated instead of opening a modal* | PR #127, symptom table row 1 |
 * | 16 | the empty-state message shown for a filter that matched nothing | PR #127, *"Two traps found while collapsing those"* |
 * | 15 | `hostname: 1` shown where a name belonged | PR #127, *"Also"*; named in #133 |
 * | 18 | a lookup answering `{}` took the whole page down | PR #127, *"Also"* |
 *
 * ## What connects 15 and 18
 *
 * They are one line apart in `logs/LogFilters.tsx` and they are opposite
 * failures of the same lookup. 15 is the lookup **not being consulted**
 * — an id rendered where the name it stands for was available all along.
 * 18 is the lookup **being trusted too far** — `?? []` guards `null` and
 * `undefined` but not `{}`, so a lookup that answered an object threw
 * inside `rows.map` during render and, with no error boundary over the
 * host root, took the entire log surface down because a *filter option
 * list* was the wrong shape.
 *
 * 18 is the one worth reading the fixtures for. `LogSearchPage.test.tsx`
 * stubs every non-events URL with `{}`, so its whole file is already
 * running against the shape that used to crash — which means the guard
 * is *load-bearing there today* and would go red if it were removed.
 * That is real coverage and it is **borrowed rather than owned**: it
 * holds only while that file's catch-all stub keeps returning `{}`, and
 * a maintainer tidying it to `[]` — the realistic shape — would remove
 * the coverage without touching a test name or a line of source. So the
 * test below states the property with a fixture chosen *for* it, and
 * says which reading it is.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
} from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { LogSearchPage } from '../LogSearchPage';
import { queryClient } from '../queryClient';
import { board, device as boardDevice, hostname } from './fixtures';
import { page, row } from './logFixtures';
import type { EventPage } from '../api/events';

const TENANT: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [
    'atrium_ddns.device.manage',
    'atrium_ddns.domain.manage',
    'atrium_ddns.hostname.manage',
  ],
  impersonating_from: null,
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let handles: MockAtriumHandles;

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // Every test here is driven from the address bar. A leftover `?zone=`
  // opens the next one on a filtered board, which fails as "the table
  // did not render" rather than as the thing it is.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------ */
/* The board                                                           */
/* ------------------------------------------------------------------ */

/** Two devices in two different zones, so a device filter and a zone
 *  filter can be made to disagree — which is how a filter matching
 *  nothing is reached without driving a Mantine combobox, and without a
 *  fixture that has nothing in it to begin with. */
const ROUTER = { id: 7, name: 'home-router', zone: 'example.net' };
const GARAGE = { id: 12, name: 'garage-pi', zone: 'example.org' };

const TWO_DEVICE_BOARD = board({
  devices: [
    boardDevice({
      id: ROUTER.id,
      name: ROUTER.name,
      hostnames: [
        hostname({
          id: 1,
          name: `host-a.${ROUTER.zone}`,
          domain_name: ROUTER.zone,
          device_id: ROUTER.id,
        }),
      ],
    }),
    boardDevice({
      id: GARAGE.id,
      name: GARAGE.name,
      hostnames: [
        hostname({
          id: 2,
          name: `host-b.${GARAGE.zone}`,
          domain_name: GARAGE.zone,
          device_id: GARAGE.id,
        }),
      ],
    }),
  ],
});

function stubBoard(payload: unknown) {
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(payload);
      if (url.endsWith('/atrium_ddns/devices')) return json([]);
      if (url.endsWith('/atrium_ddns/domains')) return json([]);
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      return json({});
    }),
  );
}

async function openBoard(search = '') {
  window.history.pushState({}, '', `/atrium-ddns${search}`);
  renderWithAtrium(<DeviceBoardPage />);
  await screen.findByTestId('board-table');
}

describe('a filter that matches nothing is a measurement, not an empty account — PR #127', () => {
  beforeEach(() => {
    queryClient.clear();
    stubBoard(TWO_DEVICE_BOARD);
  });

  test('the account-is-empty sentence does not appear over a filtered board', async () => {
    // The reported defect, stated as the thing that must not happen.
    // Keyed on the *filtered* rows, a filter matching nothing announced
    // *"You have no devices yet"* to an operator with plenty — two
    // different facts in one string, and the more alarming one shown for
    // the more ordinary cause.
    //
    // `?onlyDevice=` and `?zone=` seed the table's own two filters, so
    // this reaches zero rows through the real filter state rather than
    // through an empty payload. The device is in `example.net` and the
    // zone filter says `example.org`: both filters match something on
    // their own and nothing together.
    await openBoard(`?onlyDevice=${ROUTER.id}&zone=${GARAGE.zone}`);

    expect(
      screen.queryByTestId('board-empty'),
      'the board told an operator with two devices that they have none, ' +
        'because the empty state is keyed on the filtered rows again',
    ).toBeNull();
    expect(screen.getByTestId('board-no-match')).toBeInTheDocument();
  });

  test('it names its denominator, so a narrow result is readable', async () => {
    // `0 of 2` is a statement; an empty table under an unremarked filter
    // is not. Asserting the presence of `board-no-match` alone would
    // pass against a bare "nothing here".
    await openBoard(`?onlyDevice=${ROUTER.id}&zone=${GARAGE.zone}`);
    expect(screen.getByTestId('board-filter-count').textContent).toContain(
      'showing 0 of 2',
    );
    expect(screen.getByTestId('board-no-match').textContent).toContain(
      '2 in total',
    );
  });

  test('the filter controls stay on screen, so there is a way out', async () => {
    // The half of the report that is easy to drop. The old empty state
    // replaced the whole table *including the filter controls*, so the
    // only way back to your devices was to reload the page. The controls
    // being present is what makes the sentence above actionable.
    await openBoard(`?onlyDevice=${ROUTER.id}&zone=${GARAGE.zone}`);
    expect(screen.getByTestId('board-filters')).toBeInTheDocument();
    expect(screen.getByTestId('board-filter-device')).toBeInTheDocument();
    expect(screen.getByTestId('board-filter-zone')).toBeInTheDocument();
  });

  test('a filter that matches something is not a no-match either', async () => {
    // Control 1. Without it, `board-no-match` rendering unconditionally
    // would satisfy all three tests above.
    await openBoard(`?onlyDevice=${ROUTER.id}`);
    expect(screen.queryByTestId('board-no-match')).toBeNull();
    expect(screen.queryByTestId('board-empty')).toBeNull();
    expect(screen.getByTestId('board-filter-count').textContent).toContain(
      'showing 1 of 2',
    );
  });
});

describe('clear clears every filter, including the one it forgot — #141', () => {
  beforeEach(() => {
    queryClient.clear();
    stubBoard(TWO_DEVICE_BOARD);
  });

  /** A zone with no names in it — not on the board's payload at all. So
   *  `?zone=` reaches zero rows the way a tenant reaches them: by
   *  following the zones list's link to an empty zone, rather than by
   *  composing two filters that disagree, which is how the tests above
   *  get there. Both arrivals render the same sentence; only this one is
   *  a single filter, which is what makes the defect legible. */
  const EMPTY_ZONE = 'example.com';

  test('a zone filter from ?zone= is cleared, so the empty state is escapable', async () => {
    // The reported defect. `board-no-match` tells the tenant to "clear
    // the filter to see them", and the control beside it reset the
    // device and name filters only — so the one arrival that reliably
    // produces this screen is the one arrival its instruction cannot
    // answer, and the row count stayed at `showing 0 of 2` with no way
    // back short of editing the address bar.
    await openBoard(`?zone=${EMPTY_ZONE}`);
    expect(screen.getByTestId('board-no-match')).toBeInTheDocument();
    expect(screen.getByTestId('board-filter-count').textContent).toContain(
      'showing 0 of 2',
    );

    fireEvent.click(screen.getByTestId('board-filter-clear'));

    await waitFor(() =>
      expect(
        screen.queryByTestId('board-no-match'),
        'clear left the zone filter set. The board still matches no rows ' +
          'and the only way out is the address bar — #141.',
      ).toBeNull(),
    );
    // …and the rows it was hiding are the rows that come back. Asserting
    // only that the sentence went would pass for a clear that emptied
    // the table rather than unfiltering it. Counted against the same
    // denominator the sentence quoted — `2 in total` — rather than
    // against two written-out testids, which would pin the address
    // family into an assertion that is not about families.
    //
    // Read off the rows' own testids rather than by text: every name is
    // also an option in the Name picker, so `getByText` finds two of
    // each and fails for a reason that has nothing to do with #141.
    const rowIds = screen
      .getAllByTestId(/^board-row-/)
      .map((el) => el.getAttribute('data-testid') ?? '');
    expect(rowIds).toHaveLength(2);
    expect(
      rowIds.filter((id) => id.includes(`host-a.${ROUTER.zone}`)),
    ).toHaveLength(1);
    expect(
      rowIds.filter((id) => id.includes(`host-b.${GARAGE.zone}`)),
    ).toHaveLength(1);
  });

  test('the clear control removes itself, which is the predicate that summoned it', async () => {
    // The assertion that does not enumerate filters, and the reason a
    // fourth filter does not need a fourth test here. `board-filter-clear`
    // and `board-filter-count` are rendered behind the *same* `filtered`
    // predicate the clear handler exists to falsify, so "the button is
    // still on screen after being pressed" is exactly "some filter
    // survived" — whichever one it is, named or not.
    await openBoard(`?onlyDevice=${ROUTER.id}&zone=${EMPTY_ZONE}`);
    expect(screen.getByTestId('board-filter-clear')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('board-filter-clear'));

    await waitFor(() =>
      expect(
        screen.queryByTestId('board-filter-clear'),
        'the clear control survived its own click, so at least one filter ' +
          'it does not know about is still set — #141.',
      ).toBeNull(),
    );
    expect(screen.queryByTestId('board-filter-count')).toBeNull();
    // The controls themselves stay. Clearing a filter is not hiding the
    // means to set another one — PR #127's half of this screen.
    expect(screen.getByTestId('board-filters')).toBeInTheDocument();
  });

  test('the control is absent on an unfiltered board — the control', async () => {
    // Without this, a clear button that never rendered at all would
    // satisfy the assertion above by never being found in the first
    // place.
    await openBoard();
    expect(screen.queryByTestId('board-filter-clear')).toBeNull();
    expect(screen.queryByTestId('board-filter-count')).toBeNull();
  });
});

describe('an account with no devices still says so — the control for the above', () => {
  beforeEach(() => {
    queryClient.clear();
    stubBoard(board({ devices: [], unassigned_hostnames: [] }));
  });

  test('the empty state is about the account, and is reachable', async () => {
    // Control 2, and the reason the fix is "key it on `allRows`" rather
    // than "delete the empty state". A tenant with no devices must still
    // be told what to do — the sentence is an invitation, and the tests
    // above must not have made it unreachable.
    window.history.pushState({}, '', '/atrium-ddns');
    renderWithAtrium(<DeviceBoardPage />);
    const empty = await screen.findByTestId('board-empty');
    expect(empty.textContent).toContain('You have no devices yet');
    // …and it is *not* the filtered sentence wearing the same testid.
    expect(screen.queryByTestId('board-no-match')).toBeNull();
    expect(screen.queryByTestId('board-table')).toBeNull();
  });
});

describe('“Add a device” opens a modal on the board — PR #127 symptom 1', () => {
  beforeEach(() => {
    queryClient.clear();
    stubBoard(TWO_DEVICE_BOARD);
  });

  test('its href stays on the board, and does not leave for another page', async () => {
    // Reported: the control navigated instead of opening a modal,
    // because the create form lived on `/atrium-ddns/devices` — a page
    // with no nav entry, which is symptom 2 of the same table.
    //
    // Asserted on the href's *path*, not on its full string: the point is
    // that it does not leave the board, and pinning the query spelling
    // here would fail for a rename that is not this defect.
    await openBoard();
    const href = screen.getByTestId('board-add-device').getAttribute('href');
    expect(href, 'the add-a-device control is not a link at all').not.toBeNull();
    const url = new URL(href as string, 'https://host.invalid');
    expect(
      url.pathname,
      'the add-a-device control points off the board again. The create ' +
        'form lived on /atrium-ddns/devices, a page with no nav entry, ' +
        'and finishing a create there stranded you — PR #127.',
    ).toBe('/atrium-ddns');
  });

  test('following it opens the create form, on the board it was clicked from', async () => {
    // The reading that spans the href *and* the page's parser. jsdom does
    // not navigate, so the anchor's own href is followed by hand — the
    // same technique and the same reason as `boardAffordance.test.tsx`.
    // `device-name` is the create modal's own field: the board's table is
    // still mounted behind it, which is what "a modal, not a page" means.
    await openBoard();
    const href = screen.getByTestId('board-add-device').getAttribute('href');
    act(() => {
      window.history.pushState({}, '', href as string);
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await screen.findByTestId('device-name');
    expect(
      screen.getByTestId('board-table'),
      'the create form replaced the board rather than opening over it',
    ).toBeInTheDocument();
  });

  test('the board is bare until it is asked for — the control', async () => {
    // Without this, a create modal rendered unconditionally would pass
    // the test above.
    await openBoard();
    expect(screen.queryByTestId('device-name')).toBeNull();
  });
});

/* ------------------------------------------------------------------ */
/* The log                                                             */
/* ------------------------------------------------------------------ */

const LOG_DEVICE = { id: 7, name: 'home-router' };
const LOG_NAME = { id: 41, name: 'host-a.example.net' };
const LOG_ZONE = { id: 3, name: 'example.net' };

let logPayload: EventPage;
/** What `GET /atrium_ddns/{devices,hostnames,domains}` answers. Held as
 *  `unknown` so a test can hand back the wrong *shape* — which is the
 *  whole subject of the last describe. */
let lookups: unknown;

function stubLog() {
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/events')) return json(logPayload);
      if (url.endsWith('/atrium_ddns/devices')) return json(lookups);
      if (url.endsWith('/atrium_ddns/hostnames')) return json(lookups);
      if (url.endsWith('/atrium_ddns/domains')) return json(lookups);
      return json({});
    }),
  );
}

describe('an id filter is shown as the name it stands for — PR #127, #133', () => {
  beforeEach(() => {
    queryClient.clear();
    // The lookups the chips resolve through, populated the way the real
    // endpoints populate them. `LogSearchPage.test.tsx` leaves them at
    // `{}` — which is why both of its chip assertions read an integer
    // (`device: 7`, `device: 99`) and neither of them can see this
    // behaviour at all.
    lookups = [
      { id: LOG_DEVICE.id, name: LOG_DEVICE.name },
      { id: LOG_NAME.id, name: LOG_NAME.name },
      { id: LOG_ZONE.id, name: LOG_ZONE.name },
    ];
    logPayload = page({
      rows: [row({ id: 1, device_id: LOG_DEVICE.id })],
      filters: {
        ...page().filters,
        device_id: LOG_DEVICE.id,
        hostname_id: LOG_NAME.id,
        domain_id: LOG_ZONE.id,
      },
    });
    stubLog();
  });

  test('device, name and zone chips read names, not integers', async () => {
    // The report was literally *"`hostname: 1` shown where a name
    // belonged"*. Three id filters resolve through three lookups, and
    // all three are asserted: they are three branches of one `nameFor`
    // and any of them can be dropped on its own.
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);
    await screen.findByTestId('log-ledger');

    expect(
      screen.getByTestId('log-applied-hostname_id').textContent,
      'the name filter is still rendering the row id. `hostname: 1` is ' +
        'the string the operator reported — PR #127.',
    ).toContain(LOG_NAME.name);
    expect(screen.getByTestId('log-applied-device_id').textContent).toContain(
      LOG_DEVICE.name,
    );
    expect(screen.getByTestId('log-applied-domain_id').textContent).toContain(
      LOG_ZONE.name,
    );
  });

  test('the chip is the name and not merely a string containing the id', async () => {
    // Non-vacuity. `host-a.example.net` does not contain `41`, and
    // `home-router` does not contain `7`, so these are not satisfied by
    // an id that happens to be a substring. Stated as an exclusion
    // because `toContain(name)` would also pass on `hostname: 41
    // (host-a.example.net)`, which is not what was asked for.
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-applied-hostname_id').textContent).not.toMatch(
      /\b41\b/,
    );
  });

  test('an id the lookups do not contain falls back to the id, not to a dash', async () => {
    // The other half, and the reason `nameFor` returns the id rather
    // than `—`. A deleted device, a cross-tenant read or a pasted link
    // is a real state: `device: 99` is at least true, where `device: —`
    // would claim the filter is empty while the rows below are filtered
    // by it.
    logPayload = page({
      rows: [row({ id: 1, device_id: 99 })],
      filters: { ...page().filters, device_id: 99 },
    });
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-applied-device_id').textContent).toContain(
      '99',
    );
  });
});

describe('a lookup of the wrong shape does not take the page down — PR #127', () => {
  beforeEach(() => {
    queryClient.clear();
    logPayload = page({
      rows: [row({ id: 1, device_id: LOG_DEVICE.id })],
      filters: { ...page().filters, device_id: LOG_DEVICE.id },
    });
  });

  test('an endpoint answering an object leaves the log readable', async () => {
    // `?? []` guards `null` and `undefined`. A lookup that answers `{}`
    // — an error body, an endpoint that moved, a stub — is neither, so
    // `rows.map` threw *during render*; with no error boundary over the
    // host root React unmounted everything, and the log rendered nothing
    // because a filter option list was the wrong shape.
    //
    // The assertion is that the ledger and the chips are still there.
    // Asserting "no exception" would pass against a page that rendered
    // an empty wrapper, which is exactly what the failure looked like.
    lookups = {};
    stubLog();
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);

    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-filters')).toBeInTheDocument();
    // The selects go empty and the log still reads, which is what the
    // fallback was always meant to do.
    expect(screen.getByTestId('log-applied-device_id').textContent).toContain(
      '7',
    );
  });

  test('a bare string is refused too, not only an object', async () => {
    // The guard is `Array.isArray`, not `typeof !== 'object'`. Pinning
    // only `{}` would pass against a fix keyed on the one shape that had
    // been seen — the error-message family from the contract, applied to
    // a payload.
    lookups = 'not a list';
    stubLog();
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);
    await screen.findByTestId('log-ledger');
    expect(screen.getByTestId('log-filters')).toBeInTheDocument();
  });

  test('a well-shaped lookup still resolves — the control', async () => {
    // Without this, `list()` returning `[]` unconditionally would
    // satisfy both tests above and silently disable every chip name and
    // every filter option in the product.
    lookups = [{ id: LOG_DEVICE.id, name: LOG_DEVICE.name }];
    stubLog();
    window.history.pushState({}, '', '/atrium-ddns/log');
    renderWithAtrium(<LogSearchPage />);
    await screen.findByTestId('log-ledger');
    await waitFor(() =>
      expect(
        screen.getByTestId('log-applied-device_id').textContent,
      ).toContain(LOG_DEVICE.name),
    );
  });
});
