/** One card component, two entrances — asserted by module identity.
 *
 * `docs/ops/ui-design.md` **Part III §17**:
 *
 * > One card component, two entrances. Anything else grows a second
 * > editor.
 *
 * ## Why the assertion is a substitution and not an inspection
 *
 * The obvious test — render the modal, render the route, check both
 * show the same testid — passes against two identical copies, which is
 * the failure it is supposed to catch. Two editors that were forked
 * yesterday look the same today; the whole risk is the fork drifting
 * six weeks from now, and by then a look-alike assertion still passes.
 *
 * So this file does what the backend does when it writes
 * `router.zone_contains is providers_base.zone_contains`: it asserts
 * that the *same object* is reached from both call sites. In TypeScript
 * that is spelled with `vi.mock` — the card module is replaced by a
 * sentinel, and each entrance is then rendered and checked for the
 * sentinel. A call site holding its own copy would render its own copy
 * and never see it, so the substitution is what makes the guard able to
 * fail. `#74` and `#75` made the same move in their own terms; this is
 * that argument applied to a component.
 *
 * ## The three entrances
 *
 * - `DeviceCard`: the route (`DeviceDetailPage`), the device list's row
 *   (`DevicesPage` → `DeviceList`), and the board's row
 *   (`DeviceBoardPage` → `DeviceBoard`).
 * - `ZoneModal`: `DomainsPage` renders it, and both zone addresses
 *   (`/atrium-ddns/zones/:id` and `/atrium-ddns/zones/new`) route to that
 *   same page — so there is one entrance, and the URL decides what it
 *   shows. The substitution still holds: a second zone editor grown
 *   anywhere would not be this mock.
 *
 * Each is driven the way an operator drives it — a click on the name —
 * rather than by rendering the modal component directly, because a
 * modal nothing opens is the same artefact as a metric nothing writes.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { queryClient } from '../queryClient';
import { zoneHrefParam, zoneNewHref } from '../paths';
import { board, device as boardDevice } from './fixtures';

/** The substitutes. Each renders a string that exists nowhere else in
 *  the bundle, so finding it is finding *this* module and not a
 *  coincidence. */
const DEVICE_SENTINEL = 'zz-device-card-sentinel-4f21';
const ZONE_SENTINEL = 'zz-zone-card-sentinel-8c07';

vi.mock('../tenant/DeviceCard', async () => {
  // `importOriginal` is deliberately **not** used. The point is that
  // nothing real renders: if a call site reached its own copy of the
  // card, keeping the original around would let that copy render
  // plausibly and the sentinel's absence would be the only signal.
  // Replacing outright makes the failure loud.
  const { Modal } = await import('@mantine/core');
  const DeviceCard = ({ deviceId }: { deviceId: number }) => (
    <div data-testid="device-card-substitute">
      {DEVICE_SENTINEL} {deviceId}
    </div>
  );
  return {
    DeviceCard,
    DeviceCardModal: ({
      deviceId,
      onClose,
    }: {
      deviceId: number | null;
      onClose: () => void;
    }) => (
      <Modal opened={deviceId !== null} onClose={onClose} title="Device">
        {deviceId === null ? null : <DeviceCard deviceId={deviceId} />}
      </Modal>
    ),
    refusalText: (error: unknown) => String(error),
  };
});

vi.mock('../tenant/ZoneModal', async () => {
  const { Modal } = await import('@mantine/core');
  return {
    ZoneModal: ({
      zoneId,
      opened,
      onClose,
    }: {
      zoneId: number | null;
      opened: boolean;
      onClose: () => void;
    }) => (
      <Modal opened={opened} onClose={onClose} title="Zone">
        <div data-testid="zone-card-substitute">
          {ZONE_SENTINEL} {zoneId ?? 'new'}
        </div>
      </Modal>
    ),
  };
});

const { DeviceBoardPage } = await import('../DeviceBoardPage');
const { DeviceDetailPage } = await import('../DeviceDetailPage');
const { DevicesPage } = await import('../DevicesPage');
const { DomainsPage } = await import('../DomainsPage');

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

const DEVICE_ROW = {
  id: 7,
  name: 'home-router',
  username: 'ddns-a1b2c3d4e5f6',
  created_at: '2026-08-15T10:00:00Z',
  last_seen_at: '2026-08-15T13:47:00Z',
  rate_limit_per_minute: null,
  effective_rate_limit_per_minute: 30,
  credential_origin: 'issued',
  hostname_count: 0,
};

const ZONE_ROW = {
  id: 11,
  name: 'example.invalid',
  hostname_count: 0,
  backends: [
    {
      id: 3,
      backend_type: 'route53',
      config: {},
      credentials_set: true,
      known_service: true,
    },
  ],
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
  handles = mockAtriumRegistry();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) {
        return json(
          board({ devices: [boardDevice({ id: 7, hostnames: [] })] }),
        );
      }
      if (url.endsWith('/atrium_ddns/devices')) return json([DEVICE_ROW]);
      if (url.endsWith('/atrium_ddns/domains')) return json([ZONE_ROW]);
      // `{ providers: [...] }`, the envelope `providersQuery` unwraps.
      // A bare array here resolves to `undefined` and the zones page
      // renders its *error* state, which reads like a broken page.
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: [] });
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  vi.unstubAllGlobals();
});

describe('DeviceCard has one definition and three call sites', () => {
  test('the route renders it', async () => {
    window.history.pushState({}, '', `/atrium-ddns/devices/${DEVICE_ROW.id}`);
    renderWithAtrium(<DeviceDetailPage />);
    const found = await screen.findByTestId('device-card-substitute');
    expect(found).toHaveTextContent(`${DEVICE_SENTINEL} ${DEVICE_ROW.id}`);
  });

  test('the device list opens it in a modal, and the id travels with it', async () => {
    renderWithAtrium(<DevicesPage />);
    const name = await screen.findByTestId(`open-${DEVICE_ROW.name}`);
    // The id is asserted, not just the presence of the card: a modal
    // that opened the *first* device regardless of the row clicked
    // would render the sentinel and be wrong.
    fireEvent.click(name);
    const found = await screen.findByTestId('device-card-substitute');
    expect(found).toHaveTextContent(`${DEVICE_SENTINEL} ${DEVICE_ROW.id}`);
  });

  test('the board opens it from the device name', async () => {
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');
    fireEvent.click(screen.getByTestId(`board-open-${DEVICE_ROW.name}`));
    const found = await screen.findByTestId('device-card-substitute');
    expect(found).toHaveTextContent(`${DEVICE_SENTINEL} ${DEVICE_ROW.id}`);
  });
});

describe('ZoneModal has one definition, and the URL decides what it shows', () => {
  test('a zone address opens it with that zone, before any click', async () => {
    // The operator's requirement, asserted directly: land on the address
    // — as a reload, a pasted link or a Back — and the modal is already
    // open on the right zone. A test that clicked first would pass
    // against the old `useState` version too.
    window.history.pushState({}, '', zoneHrefParam(ZONE_ROW.id));
    renderWithAtrium(<DomainsPage />);
    const found = await screen.findByTestId('zone-card-substitute');
    expect(found).toHaveTextContent(`${ZONE_SENTINEL} ${ZONE_ROW.id}`);
  });

  test('the create address opens the same modal with no zone', async () => {
    window.history.pushState({}, '', zoneNewHref());
    renderWithAtrium(<DomainsPage />);
    const found = await screen.findByTestId('zone-card-substitute');
    expect(found).toHaveTextContent(`${ZONE_SENTINEL} new`);
  });

  test('the list is bare at the list address', async () => {
    // The other direction, and the one that catches a modal wired to
    // render unconditionally: no zone in the URL, no modal.
    window.history.pushState({}, '', '/atrium-ddns/domains');
    renderWithAtrium(<DomainsPage />);
    await screen.findByTestId(`open-domain-${ZONE_ROW.name}`);
    expect(screen.queryByTestId('zone-card-substitute')).toBeNull();
  });
});

describe('the substitution is what makes the guard able to fail', () => {
  test('nothing renders the real cards while they are replaced', async () => {
    // The vacuity check for this whole file. If `vi.mock` were not
    // taking effect — a wrong specifier, a hoisting mistake, a call
    // site importing through a different path — the real cards would
    // render, the sentinels would be absent, and every test above
    // would fail *loudly*. This one fails in the other direction: it
    // catches a substitution that took effect but left the real card
    // rendering beside it, which is what a second copy at a call site
    // looks like from here.
    renderWithAtrium(<DevicesPage />);
    fireEvent.click(await screen.findByTestId(`open-${DEVICE_ROW.name}`));
    await screen.findByTestId('device-card-substitute');
    await waitFor(() => {
      expect(screen.queryByTestId('device-detail')).not.toBeInTheDocument();
    });
    // `device-detail` is the real `DeviceCard`'s own root testid, and
    // it is not rendered by the substitute. Its presence here would
    // mean something reached the real module while the mock was in
    // force — two editors, live, at the same time.
  });
});
