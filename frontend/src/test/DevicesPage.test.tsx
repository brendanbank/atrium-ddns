/** The devices surface, under `@brendanbank/atrium-test-utils`.
 *
 * Written to prove the three things the issue singles out, in the order
 * it lists them:
 *
 * 1. **The secret is shown once.** Create renders it; dismissing drops
 *    it; and the *listing that arrives afterwards does not carry it* —
 *    asserted against the whole rendered document, not against the
 *    absence of one element, because an implementation that stashed it
 *    in a second component would pass the narrower check.
 * 2. **A migrated device says so rather than rendering an empty field.**
 *    The sentence is asserted, and so is its absence for an `issued`
 *    device — a notice that is always shown is not a notice.
 * 3. **Rotate warns before the button, not after.** A router still
 *    holding the old secret starts answering `badauth` the moment the
 *    request commits, so a warning shown afterwards is a warning shown
 *    during the outage.
 *
 * …plus the permission gate in both directions, because a refusal and
 * an empty list are different facts and rendering them the same way
 * tells a refused user something untrue about their account.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DevicesPage } from '../DevicesPage';
import { DEVICE_PERMISSION, type Device } from '../api/devices';
import { queryClient } from '../queryClient';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [DEVICE_PERMISSION],
  impersonating_from: null,
};

const OUTSIDER: UserContext = {
  id: 2,
  email: 'outsider@example.com',
  full_name: 'Outsider',
  is_active: true,
  roles: [],
  // Holds *other* atrium_ddns permissions on purpose: a gate spelled
  // "holds any atrium_ddns code" passes with an empty list and fails
  // here, which is the point.
  permissions: ['atrium_ddns.domain.manage', 'atrium_ddns.hostname.manage'],
  impersonating_from: null,
};

/** The secret the API hands back exactly once. A value nothing else in
 *  this file uses, so a search for it in the document is a search for
 *  *this* secret. */
const ISSUED_SECRET = 'zz-once-only-9f3c7a1e-do-not-persist';
const ROTATED_SECRET = 'zz-rotated-4b8d2f60-do-not-persist';

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: 1,
    name: 'home-router',
    username: 'ddns-a1b2c3d4e5f6',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    // #73. Two readings, both the server's — `null` is *inherit* and
    // the resolved value beside it is what the limiter enforces.
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued',
    hostname_count: 2,
    ...overrides,
  };
}

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let devicesPayload: Device[] = [];
let devicesFetches = 0;

/** Every PATCH the page made, with its body. #73's route must carry the
 *  limit and nothing else, and the only place to see that is the
 *  request. */
let patches: { url: string; body: Record<string, unknown> }[] = [];

function stubFetch() {
  devicesFetches = 0;
  patches = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return json(currentMe);
      }
      if (url.endsWith('/atrium_ddns/devices') && method === 'GET') {
        devicesFetches += 1;
        return json(devicesPayload);
      }
      if (url.endsWith('/atrium_ddns/devices') && method === 'POST') {
        const created = device({ id: 2, name: 'new-router', hostname_count: 0 });
        devicesPayload = [...devicesPayload, created];
        return json({ device: created, secret: ISSUED_SECRET }, 201);
      }
      if (method === 'PATCH') {
        const body = JSON.parse(String(init?.body ?? '{}'));
        patches.push({ url, body });
        const updated = device({
          ...devicesPayload[0],
          rate_limit_per_minute: body.rate_limit_per_minute,
          effective_rate_limit_per_minute:
            body.rate_limit_per_minute === null
              ? 30
              : body.rate_limit_per_minute,
        });
        devicesPayload = [updated, ...devicesPayload.slice(1)];
        return json(updated);
      }
      if (url.endsWith('/rotate')) {
        const rotated = device({ ...devicesPayload[0], credential_origin: 'issued' });
        devicesPayload = [rotated, ...devicesPayload.slice(1)];
        return json({ device: rotated, secret: ROTATED_SECRET });
      }
      return json({});
    }),
  );
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  stubFetch();
  devicesPayload = [device()];
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  handles?.cleanup();
  vi.unstubAllGlobals();
  currentMe = null;
});

async function mount(user: UserContext | null) {
  currentMe = user;
  handles = mockAtriumRegistry({ me: user });
  renderWithAtrium(<DevicesPage />);
  // Waiting on the title, or on "refused OR loaded", both return too
  // early: `usePerm` answers false while `me` is still in flight, so
  // **the refusal is also the pre-resolution state**. An OR-wait
  // therefore settles on the transient refusal for a permitted user and
  // every assertion after it runs against the wrong page. Wait for the
  // marker this user is expected to end on.
  const expected = user?.permissions.includes(DEVICE_PERMISSION)
    ? 'add-device'
    : 'devices-refused';
  await waitFor(() =>
    expect(screen.getByTestId(expected)).toBeInTheDocument(),
  );
}

describe('the permission gate', () => {
  test('a holder sees the list', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-home-router')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('devices-refused')).not.toBeInTheDocument();
  });

  test('a non-holder sees a refusal, not an empty list, and fires no request', async () => {
    await mount(OUTSIDER);
    await waitFor(() =>
      expect(screen.getByTestId('devices-refused')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('devices-empty')).not.toBeInTheDocument();
    // A 403 in the network tab on every page load is noise, and the
    // refusal is already known client-side.
    expect(devicesFetches).toBe(0);
  });
});

describe('the secret is shown once', () => {
  test('create renders it, dismissal drops it, and the reload has no trace', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-device'));
    // Mantine's Modal mounts into a portal on a later tick.
    await waitFor(() =>
      expect(screen.getByTestId('device-name')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('device-name'), {
      target: { value: 'new-router' },
    });
    fireEvent.click(screen.getByTestId('device-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('issued-secret')).toHaveTextContent(
        ISSUED_SECRET,
      ),
    );
    // The sentence is a fact, not advice. Asserted because the whole
    // design of this surface rests on the user reading it.
    expect(screen.getByTestId('secret-once-warning')).toHaveTextContent(
      /only time this secret will ever be shown/i,
    );
    // The username travels with it: they are one credential and are
    // configured into the router together.
    expect(screen.getByTestId('issued-username')).toHaveTextContent(
      'ddns-a1b2c3d4e5f6',
    );

    fireEvent.click(screen.getByTestId('dismiss-secret'));

    await waitFor(() =>
      expect(screen.queryByTestId('device-secret-once')).not.toBeInTheDocument(),
    );
    // Against the *whole document*, not against one element: an
    // implementation that stashed the secret somewhere else would pass
    // the narrower check.
    expect(document.body.textContent).not.toContain(ISSUED_SECRET);
  });

  test('rotate shows a new secret and the second read carries neither', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('rotate-home-router'));

    // The warning is shown *before* the request is made.
    await waitFor(() =>
      expect(screen.getByTestId('rotate-warning')).toHaveTextContent(
        /stops the old one working immediately/i,
      ),
    );
    expect(document.body.textContent).not.toContain(ROTATED_SECRET);

    fireEvent.click(screen.getByTestId('rotate-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('issued-secret')).toHaveTextContent(
        ROTATED_SECRET,
      ),
    );

    fireEvent.click(screen.getByTestId('dismiss-secret'));
    await waitFor(() =>
      expect(screen.queryByTestId('device-secret-once')).not.toBeInTheDocument(),
    );
    expect(document.body.textContent).not.toContain(ROTATED_SECRET);
    expect(document.body.textContent).not.toContain(ISSUED_SECRET);
  });

  test('nothing offers to show a secret again', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-home-router')).toBeInTheDocument(),
    );
    // Not disabled, not greyed out — absent. A disabled control for an
    // impossible operation teaches that the operation exists and is
    // merely unavailable, which is the belief that becomes a support
    // request.
    expect(screen.queryByText(/show.*secret/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/reveal/i)).not.toBeInTheDocument();
  });
});

describe('the migrated device', () => {
  test('says the plaintext was never ours, rather than rendering a blank', async () => {
    devicesPayload = [device({ credential_origin: 'migrated' })];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-origin-migrated')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('device-origin-migrated')).toHaveTextContent(
      /never held the plaintext and cannot show it/i,
    );
  });

  test('an issued device carries no such notice', async () => {
    // A notice that is always shown is not a notice. This is the half
    // that stops the sentence becoming furniture.
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-home-router')).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId('device-origin-migrated'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('device-origin-unrecognised'),
    ).not.toBeInTheDocument();
  });

  test('an unrecognised credential is described as unable to authenticate', async () => {
    devicesPayload = [device({ credential_origin: 'unrecognised' })];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(
        screen.getByTestId('device-origin-unrecognised'),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId('device-origin-unrecognised')).toHaveTextContent(
      /cannot authenticate/i,
    );
  });
});

describe('the empty state', () => {
  test('names the next action, in the interface’s voice', async () => {
    devicesPayload = [];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('devices-empty')).toHaveTextContent(
        'You have no devices yet. Add one to get a DDNS username and password.',
      ),
    );
  });
});

describe('the per-device rate limit (#73)', () => {
  test('the stored value is displayed, and says which of three states it is', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(
        screen.getByTestId('limit-summary-home-router'),
      ).toBeInTheDocument(),
    );
    // The fixture inherits, so the number is the installation default
    // and the sentence says so. "30/min" alone would make *inherited*
    // and *set to 30* the same string, and only one of them follows a
    // change to the installation default.
    expect(screen.getByTestId('limit-summary-home-router')).toHaveTextContent(
      '30/min, inherited',
    );
  });

  test('an explicit value reads as this device’s own, and zero reads as muted', async () => {
    devicesPayload = [
      device({ rate_limit_per_minute: 5, effective_rate_limit_per_minute: 5 }),
    ];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('limit-summary-home-router')).toHaveTextContent(
        '5/min, set on this device',
      ),
    );

    cleanup();
    queryClient.clear();
    devicesPayload = [
      device({ rate_limit_per_minute: 0, effective_rate_limit_per_minute: 0 }),
    ];
    await mount(OPERATOR);
    // Words, not `0/min`. A muted device is a decision; a zero with a
    // unit after it reads as a measurement.
    await waitFor(() =>
      expect(screen.getByTestId('limit-summary-home-router')).toHaveTextContent(
        'muted — may never call',
      ),
    );
  });

  test('editing the limit PATCHes the limit and only the limit', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('limit-home-router'));
    await waitFor(() =>
      expect(screen.getByTestId('limit-input')).toBeInTheDocument(),
    );
    // Said before the button: this one does *not* break the device,
    // which is the whole reason the route exists.
    expect(screen.getByTestId('limit-explainer')).toHaveTextContent(
      /does not touch its username or its secret/i,
    );

    fireEvent.change(screen.getByTestId('limit-input'), {
      target: { value: '4' },
    });
    fireEvent.click(screen.getByTestId('limit-submit'));

    await waitFor(() => expect(patches.length).toBe(1));
    expect(patches[0].url).toContain('/atrium_ddns/devices/1');
    // Exactly one key. A body that also carried `name`, `username` or
    // anything credential-shaped is the thing this route promises not
    // to be.
    expect(Object.keys(patches[0].body)).toEqual(['rate_limit_per_minute']);
    expect(patches[0].body.rate_limit_per_minute).toBe(4);

    await waitFor(() =>
      expect(screen.getByTestId('limit-summary-home-router')).toHaveTextContent(
        '4/min, set on this device',
      ),
    );
  });

  test('an emptied box sends null — inherit — and never zero', async () => {
    devicesPayload = [
      device({ rate_limit_per_minute: 9, effective_rate_limit_per_minute: 9 }),
    ];
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('limit-home-router'));
    await waitFor(() =>
      expect(screen.getByTestId('limit-input')).toBeInTheDocument(),
    );
    // Seeded from the stored value, not the effective one.
    expect((screen.getByTestId('limit-input') as HTMLInputElement).value).toBe(
      '9',
    );
    fireEvent.change(screen.getByTestId('limit-input'), {
      target: { value: '' },
    });
    fireEvent.click(screen.getByTestId('limit-submit'));

    await waitFor(() => expect(patches.length).toBe(1));
    // `null`, not `0`. Coercing an empty box to zero would mute the
    // device — the opposite of what the user asked for, and silent.
    expect(patches[0].body.rate_limit_per_minute).toBeNull();
  });
});
