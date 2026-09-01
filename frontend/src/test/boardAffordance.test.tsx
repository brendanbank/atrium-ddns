/** The board's per-row add-a-name `+`, end to end, for both row shapes.
 *
 * ## What is under test, and why it needs four files rather than one
 *
 * Clicking a row's `+` opens the name form with **that row's device
 * already chosen**. The behaviour is one sentence and four files:
 *
 * | file | its half of the contract |
 * |---|---|
 * | `board/BoardTable.tsx` | passes `row.device?.id` into `boardNameNewHref` |
 * | `paths.ts` | spells it `&for=<id>` (`NAME_FOR_PARAM`) |
 * | `DeviceBoardPage.tsx` | parses `for=` back out into `presetDeviceId` |
 * | `tenant/NameModal.tsx` | seeds the device `Select` from it |
 *
 * `returnAddress.test.tsx` already covers the middle one — it asserts
 * `boardNameNewHref(7) === '/atrium-ddns?name=new&for=7'`. That is a
 * test of the **spelling**, and the spelling was never the fragile
 * part: a pure function with a literal expectation cannot drift
 * silently. What had no test at all is whether anything *passes* a
 * device id in, and whether anything *reads* one out — so
 * `boardNameNewHref()` called with no argument from a row that knows
 * perfectly well which device it is would keep every existing test
 * green and ship the exact defect that was reported by hand:
 *
 *   *"When I click the add icon after the name it does not autofill the
 *   device in the name card."*
 *
 * So these tests deliberately do **not** assert on the href string.
 * They assert on the *seeded select*, which is the only reading that
 * spans all four files, and they get there the way a browser does:
 * follow the anchor's own `href`.
 *
 * ## Why both rows, separately
 *
 * `flatten` in `BoardTable.tsx` emits a row per name **and** a row per
 * device with no names (`hostname: null`). Those are two branches, and
 * `BoardTable.tsx` draws two different affordances for them —
 * `board-add-name-<name>` and `board-add-name-for-<device>` — with two
 * separate `boardNameNewHref(row.device?.id)` call sites. Either can be
 * refactored while the other stays green, so neither test stands in for
 * the other.
 *
 * The "no names yet" row is the one that matters most: it is the state
 * the board's own empty text tells you to fix, and it is the row where
 * an empty device select is least likely to be noticed, because there
 * is no name on the row to compare it against.
 *
 * ## Non-vacuity — what a passing run rules out
 *
 * Three devices, and the expected answer is never the first of
 * anything:
 *
 *  - `shed-ap` (id 3) is **first** in both the board and the device
 *    dropdown, and is the answer to neither test. A seed that took the
 *    first option, or the first device, fails both.
 *  - `shed-ap` is also the **first no-names row**, so the "no names
 *    yet" test cannot pass by picking the first row of its own kind
 *    either — it expects `garage-pi`, the second.
 *  - `Not assigned` is the `Select`'s own default (`UNASSIGNED`), so
 *    *no seeding at all* renders a real, plausible string rather than a
 *    blank. Asserting the device name rules that out; asserting
 *    "not empty" would not.
 *
 * ## What it does not assert
 *
 * Not that clicking the anchor navigates — jsdom does not implement
 * navigation, and a test that pretended otherwise would be asserting
 * about its own harness. `follow()` does what the browser would do with
 * the `href` the component actually rendered, and reads that href from
 * the DOM rather than recomputing it, so a wrong href is followed
 * faithfully to a wrong destination and the assertion fails there.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { queryClient } from '../queryClient';
import type { CredentialOrigin, Device } from '../api/devices';
import { board, device as boardDevice, hostname } from './fixtures';

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

/** The decoy. First on the board, first in the dropdown, and the
 *  expected answer to nothing — see "Non-vacuity" above. */
const SHED = { id: 3, name: 'shed-ap' };
/** The device on the **populated** name row. */
const ROUTER = { id: 7, name: 'home-router' };
/** The device on the **"no names yet"** row under test. Second of the
 *  two nameless devices, deliberately. */
const GARAGE = { id: 12, name: 'garage-pi' };

/** The name that hangs off `ROUTER`, and the row the populated-row test
 *  clicks. Its `+` is `board-add-name-<name>`. */
const NAME = 'host-a.example.net';

function apiDevice(id: number, name: string): Device {
  return {
    id,
    name,
    username: `ddns-${String(id).padStart(12, '0')}`,
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued' as CredentialOrigin,
    hostname_count: 0,
  };
}

/** Board and dropdown agree on the same three devices, in the same
 *  order. The decoy is first in both. */
const BOARD = board({
  devices: [
    boardDevice({ id: SHED.id, name: SHED.name, hostnames: [] }),
    boardDevice({
      id: ROUTER.id,
      name: ROUTER.name,
      hostnames: [hostname({ id: 1, name: NAME, device_id: ROUTER.id })],
    }),
    boardDevice({ id: GARAGE.id, name: GARAGE.name, hostnames: [] }),
  ],
});

const DEVICES = [
  apiDevice(SHED.id, SHED.name),
  apiDevice(ROUTER.id, ROUTER.name),
  apiDevice(GARAGE.id, GARAGE.name),
];

const ZONE = {
  id: 11,
  name: 'example.net',
  hostname_count: 1,
  backends: [],
};

let handles: MockAtriumHandles;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  queryClient.clear();
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(BOARD);
      if (url.endsWith('/atrium_ddns/devices')) return json(DEVICES);
      if (url.endsWith('/atrium_ddns/domains')) return json([ZONE]);
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: [] });
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // These tests navigate. `DeviceBoardInner` reads every modal out of
  // the address bar, so leaving `?name=new&for=12` behind would open the
  // next test on a board with a modal already over it — which fails as
  // "the table did not render", the wrong diagnosis entirely.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/** Do what the browser would do with an anchor: go where its `href`
 *  says.
 *
 * `component="a"` renders a real anchor precisely so the address bar is
 * the state, but jsdom does not implement navigation — `click()` on an
 * anchor logs "Not implemented" and leaves `window.location` alone. So
 * the navigation is performed here instead, from the `href` **read out
 * of the DOM**, never recomputed. `useAtriumLocation` subscribes to
 * `popstate`, which is how atrium's own router re-syncs, so the
 * dispatch below is the same signal the running app sees.
 *
 * The href is asserted non-null first: `getAttribute` returns `null` for
 * a missing attribute, and `pushState(…, null)` navigates to the string
 * `"null"` — a URL with no `name=` at all, on which the modal never
 * opens and the test fails with a timeout rather than with the reason.
 */
function follow(anchor: HTMLElement) {
  const href = anchor.getAttribute('href');
  expect(
    href,
    'the add-a-name affordance rendered without an href, so there is ' +
      'nothing to follow — it is not a link',
  ).not.toBeNull();
  act(() => {
    window.history.pushState({}, '', href as string);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
}

/** The device `Select`'s visible reading. Mantine renders the selected
 *  option's **label**, so this is the device's name, not its id — which
 *  is also what the operator's report was about ("it does not autofill
 *  the device"). */
async function seededDevice(): Promise<HTMLInputElement> {
  await screen.findByTestId('name-modal-body');
  return (await screen.findByTestId('hostname-device')) as HTMLInputElement;
}

describe('the board’s per-row add-a-name `+` preselects that row’s device', () => {
  test('from a populated name row', async () => {
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');

    // The affordance beside the name itself — `flatten`'s
    // `hostname !== null` branch.
    follow(screen.getByTestId(`board-add-name-${NAME}`));

    const select = await seededDevice();
    await waitFor(() =>
      // `.value`, not `toHaveValue(select)`: jest-dom's element matcher
      // prints the whole rendered board on failure, and a guard that
      // fails uninformatively gets deleted rather than investigated.
      // This one prints `expected 'Not assigned' to be 'home-router'`.
      expect(
        select.value,
        `the name form opened without ${ROUTER.name} chosen — the row's ` +
          'device did not survive the trip through the address bar',
      ).toBe(ROUTER.name),
    );
  });

  test('from a "no names yet" row', async () => {
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');

    // `flatten`'s `hostname === null` branch: a device with no names at
    // all. A different call site, a different testid, and the row where
    // an unseeded select is hardest to notice. Two of them exist on this
    // board (`shed-ap` and `garage-pi`) and the second is the one taken.
    follow(screen.getByTestId(`board-add-name-for-${GARAGE.name}`));

    const select = await seededDevice();
    await waitFor(() =>
      expect(
        select.value,
        `the name form opened without ${GARAGE.name} chosen — the "no ` +
          'names yet" row reaches the affordance through a different ' +
          'branch of `flatten` and has its own call site',
      ).toBe(GARAGE.name),
    );
  });

  test('the header’s add-a-name carries no device, and that is not a bug', async () => {
    // The control against which the two above are a measurement. The
    // header `+` belongs to no row, so it has no device to name and the
    // form must open unassigned. Without this, a "seed the device to
    // *something*" implementation would satisfy both tests above and be
    // wrong here — and `Not assigned` is a real option, so the
    // difference is visible rather than being an absence.
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');

    follow(screen.getByTestId('board-add-name'));

    const select = await seededDevice();
    await waitFor(() => expect(select.value).toBe('Not assigned'));
  });
});
