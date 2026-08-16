/** `/atrium-ddns/devices/:id` — #89, `ui-design.md` §11.2.
 *
 * Written to make four weaker implementations fail:
 *
 * 1. **A rename that hides the conflict.** The 409 is rendered *as the
 *    server wrote it*, including the offending name. An implementation
 *    that caught the conflict and retried with `router (2)` passes every
 *    "the rename worked" assertion; this file asserts the server's own
 *    sentence is on screen and that the name on the page did not move.
 * 2. **A rename that carries the secret.** The PATCH body is inspected
 *    key by key. `name` and `rate_limit_per_minute`, and nothing else —
 *    a body that grew a `secret` or a `username` is the thing this
 *    route promises not to be.
 * 3. **A rename that pins an inheriting device.** The limit re-sent with
 *    the name has to be the **stored** value (`null`), never the
 *    resolved one (`30`). The two are indistinguishable on a device that
 *    has an explicit limit set, so the fixture inherits.
 * 4. **A rate-limit control where *inherit* is an omission.** `null` is
 *    a value on that field, and the radio is how it is chosen. Asserted
 *    by driving the control and reading the request.
 *
 * The URL is set with `history.pushState` because the page reads
 * `window.location` rather than `useParams` — the host bundle mounts its
 * own React tree, so react-router's context does not cross the boundary.
 * Setting it is therefore part of the arrangement, not a shortcut around
 * one.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceDetailPage } from '../DeviceDetailPage';
import { DEVICE_PERMISSION, type Device } from '../api/devices';
import { deviceHref } from '../paths';
import { queryClient } from '../queryClient';
import { board, device as boardDevice, hostname } from './fixtures';

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
  permissions: ['atrium_ddns.domain.manage'],
  impersonating_from: null,
};

const ROTATED_SECRET = 'zz-detail-rotated-51c9ea77-do-not-persist';

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: 1,
    name: 'home-router',
    username: 'ddns-a1b2c3d4e5f6',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    // Inherits on purpose: it is the only state in which "the stored
    // value was re-sent" and "the effective value was re-sent" produce
    // different requests.
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued',
    hostname_count: 1,
    ...overrides,
  };
}

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let payload: Device = device();
/** The next PATCH's answer. `null` means "echo the request". */
let patchRefusal: { status: number; body: unknown } | null = null;
let patches: { url: string; body: Record<string, unknown> }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
  patches = [];
  patchRefusal = null;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return json(currentMe);
      }
      if (url.endsWith('/atrium_ddns/board')) {
        return json(
          board({
            devices: [
              boardDevice({
                id: payload.id,
                name: payload.name,
                hostnames: [hostname({ id: 7, name: 'home.example.invalid' })],
              }),
            ],
          }),
        );
      }
      if (method === 'PATCH') {
        const body = JSON.parse(String(init?.body ?? '{}'));
        patches.push({ url, body });
        if (patchRefusal) {
          const refusal = patchRefusal;
          patchRefusal = null;
          return json(refusal.body, refusal.status);
        }
        payload = device({
          ...payload,
          ...(typeof body.name === 'string' ? { name: body.name } : {}),
          rate_limit_per_minute: body.rate_limit_per_minute,
          effective_rate_limit_per_minute:
            body.rate_limit_per_minute === null ? 30 : body.rate_limit_per_minute,
        });
        return json(payload);
      }
      if (url.endsWith('/rotate')) {
        return json({ device: payload, secret: ROTATED_SECRET });
      }
      if (/\/atrium_ddns\/devices\/\d+$/.test(url) && method === 'GET') {
        const id = Number(url.split('/').pop());
        if (id !== payload.id) {
          return json({ detail: 'no such device' }, 404);
        }
        return json(payload);
      }
      return json({});
    }),
  );
}

async function mount(
  user: UserContext | null,
  path: string = deviceHref(1),
) {
  currentMe = user;
  window.history.pushState({}, '', path);
  handles = mockAtriumRegistry({ me: user });
  renderWithAtrium(<DeviceDetailPage />);
}

beforeEach(() => {
  payload = device();
  stubFetch();
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  handles?.cleanup();
  vi.unstubAllGlobals();
  currentMe = null;
  window.history.pushState({}, '', '/');
});

describe('arriving on the route', () => {
  test('a linked URL renders the device it names', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-name')).toHaveTextContent(
        'home-router',
      ),
    );
    // §11.2's header line: the username and the two timestamps.
    expect(screen.getByTestId('detail-username')).toHaveTextContent(
      'ddns-a1b2c3d4e5f6',
    );
    expect(screen.getByTestId('detail-back')).toHaveAttribute(
      'href',
      '/atrium-ddns/devices',
    );
  });

  test('the strips render at full width, from the board', async () => {
    await mount(OPERATOR);
    // The signature element, on the detail route. §12's whole argument
    // is that a drawer at 620px would wrap it.
    await waitFor(() =>
      expect(
        screen.getByTestId('hostname-home.example.invalid'),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('detail-no-names')).not.toBeInTheDocument();
  });

  test("another tenant's device is a missing device, said in those words", async () => {
    // The server answers 404 for *not yours* as well as for *no such
    // row*, deliberately — a 403 would confirm the row exists. The page
    // must not render that as a load failure, which reads as a bug.
    await mount(OPERATOR, deviceHref(999));
    // Longer than the default 1000ms on purpose: `queryClient` is
    // configured `retry: 1`, so the 404 is fetched twice with react
    // query's exponential back-off between the attempts, and the page
    // is legitimately still `Loading…` for the first second. Shortening
    // this by disabling the retry would test a client the bundle does
    // not ship.
    await waitFor(
      () => expect(screen.getByTestId('detail-error')).toBeInTheDocument(),
      { timeout: 5_000 },
    );
    expect(screen.getByTestId('detail-error')).toHaveTextContent(
      /no such device/i,
    );
  });

  test('a non-numeric id is refused before a request is made', async () => {
    await mount(OPERATOR, '/atrium-ddns/devices/not-an-id');
    await waitFor(() =>
      expect(screen.getByTestId('detail-bad-url')).toBeInTheDocument(),
    );
  });

  test('a caller without the permission sees a refusal, not a missing device', async () => {
    await mount(OUTSIDER);
    await waitFor(() =>
      expect(screen.getByTestId('detail-refused')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('detail-error')).not.toBeInTheDocument();
  });
});

describe('the name is editable, in place', () => {
  test('renaming PATCHes name plus the stored limit, and nothing else', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-rename')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('device-rename'));

    // In place, at the top of the route — not in a modal. Mantine's
    // `Modal` renders into a portal with `role="dialog"`; the absence of
    // one is the assertion.
    await waitFor(() =>
      expect(screen.getByTestId('device-name-input')).toBeInTheDocument(),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: 'garage-router' },
    });
    fireEvent.click(screen.getByTestId('device-name-save'));

    await waitFor(() => expect(patches.length).toBe(1));
    // Two keys, and the reason the second one is there: the server
    // *requires* `rate_limit_per_minute` (#73 — `null` is a value, so
    // an omitted key and an explicit null would be one request).
    expect(Object.keys(patches[0].body).sort()).toEqual([
      'name',
      'rate_limit_per_minute',
    ]);
    expect(patches[0].body.name).toBe('garage-router');
    // The **stored** value, not the resolved one. `30` here would pin
    // an inheriting device to today's installation default — a rename
    // that quietly stops a device following a setting.
    expect(patches[0].body.rate_limit_per_minute).toBeNull();

    await waitFor(() =>
      expect(screen.getByTestId('device-name')).toHaveTextContent(
        'garage-router',
      ),
    );
  });

  test('the name is trimmed and a blank one cannot be submitted', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-rename')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('device-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('device-name-input')).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: '   ' },
    });
    expect(screen.getByTestId('device-name-save')).toBeDisabled();
    expect(patches.length).toBe(0);

    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: '  spaced-router  ' },
    });
    fireEvent.click(screen.getByTestId('device-name-save'));
    await waitFor(() => expect(patches.length).toBe(1));
    expect(patches[0].body.name).toBe('spaced-router');
  });

  test('Cancel restores the stored name and sends nothing', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-rename')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('device-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('device-name-input')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: 'never-saved' },
    });
    fireEvent.click(screen.getByTestId('device-name-cancel'));

    await waitFor(() =>
      expect(screen.getByTestId('device-name')).toHaveTextContent(
        'home-router',
      ),
    );
    expect(patches.length).toBe(0);
    expect(document.body.textContent).not.toContain('never-saved');
  });
});

describe('the conflict is surfaced, not avoided', () => {
  test("a 409 renders the server's own sentence and the name does not move", async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-rename')).toBeInTheDocument(),
    );
    patchRefusal = {
      status: 409,
      body: { detail: "you already have a device called 'garage-router'" },
    };
    fireEvent.click(screen.getByTestId('device-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('device-name-input')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: 'garage-router' },
    });
    fireEvent.click(screen.getByTestId('device-name-save'));

    await waitFor(() =>
      expect(screen.getByTestId('device-name-refusal')).toBeInTheDocument(),
    );
    // Verbatim, including the offending name. Not "that name is in use",
    // which is this component's words about the server's answer.
    expect(screen.getByTestId('device-name-refusal')).toHaveTextContent(
      "you already have a device called 'garage-router'",
    );
    // Exactly one attempt: nothing retried behind the refusal with a
    // generated suffix.
    expect(patches.length).toBe(1);
    expect(
      patches.filter((patch) => String(patch.body.name).includes('(2)')),
    ).toEqual([]);
    // The editor stays open on the refused draft, so the operator can
    // correct it rather than retype it.
    expect(
      (screen.getByTestId('device-name-input') as HTMLInputElement).value,
    ).toBe('garage-router');
  });
});

describe('the rate limit keeps its third state', () => {
  test('inherit is a choice, and it names the default only when it can', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('detail-limit-current')).toBeInTheDocument(),
    );
    // Inheriting: the resolved number *is* the installation default, so
    // it can be named.
    expect(screen.getByTestId('detail-limit-current')).toHaveTextContent(
      '30/min, inherited',
    );
    expect(document.body.textContent).toContain(
      'inherit the installation default (30)',
    );

    // Choosing a per-device value sends the number.
    fireEvent.click(screen.getByTestId('detail-limit-own'));
    fireEvent.change(screen.getByTestId('detail-limit-input'), {
      target: { value: '4' },
    });
    fireEvent.click(screen.getByTestId('detail-limit-save'));
    await waitFor(() => expect(patches.length).toBe(1));
    // No `name` key: this is #73's route, unchanged, and a limit change
    // must not be able to rename anything.
    expect(Object.keys(patches[0].body)).toEqual(['rate_limit_per_minute']);
    expect(patches[0].body.rate_limit_per_minute).toBe(4);

    await waitFor(() =>
      expect(screen.getByTestId('detail-limit-current')).toHaveTextContent(
        '4/min, set on this device',
      ),
    );
    // …and now the default is *not* knowable, so no number is invented.
    expect(document.body.textContent).toContain(
      'inherit the installation default',
    );
    expect(document.body.textContent).not.toContain(
      'inherit the installation default (30)',
    );
  });

  test('choosing inherit sends null, and it is a choice rather than an empty box', async () => {
    payload = device({
      rate_limit_per_minute: 9,
      effective_rate_limit_per_minute: 9,
    });
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('detail-limit-input')).toBeInTheDocument(),
    );
    // Seeded from the stored value.
    expect(
      (screen.getByTestId('detail-limit-input') as HTMLInputElement).value,
    ).toBe('9');
    fireEvent.click(screen.getByTestId('detail-limit-inherit'));
    fireEvent.click(screen.getByTestId('detail-limit-save'));

    await waitFor(() => expect(patches.length).toBe(1));
    // `null`, not `0`. The two are different states and only one of
    // them is *may never call*.
    expect(patches[0].body.rate_limit_per_minute).toBeNull();
  });
});

describe('rotation is its own operation', () => {
  test('it is not on the name’s Save button, and it warns before it commits', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('detail-rotate')).toBeInTheDocument(),
    );
    // The consequence is stated on the page, beside the control, before
    // anything is clicked.
    expect(screen.getByTestId('detail-rotate-consequence')).toHaveTextContent(
      /stops working until it is reconfigured/i,
    );

    fireEvent.click(screen.getByTestId('detail-rotate'));
    await waitFor(() =>
      expect(screen.getByTestId('detail-rotate-warning')).toHaveTextContent(
        /stops the old one working immediately/i,
      ),
    );
    // Nothing has been issued yet.
    expect(document.body.textContent).not.toContain(ROTATED_SECRET);

    fireEvent.click(screen.getByTestId('detail-rotate-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('issued-secret')).toHaveTextContent(
        ROTATED_SECRET,
      ),
    );
    fireEvent.click(screen.getByTestId('dismiss-secret'));
    await waitFor(() =>
      expect(
        screen.queryByTestId('device-secret-once'),
      ).not.toBeInTheDocument(),
    );
    // Against the whole document: an implementation that stashed it
    // somewhere else would pass a narrower check.
    expect(document.body.textContent).not.toContain(ROTATED_SECRET);
  });

  test('no rename request ever carries a secret-shaped key', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('device-rename')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('device-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('device-name-input')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('device-name-input'), {
      target: { value: 'renamed' },
    });
    fireEvent.click(screen.getByTestId('device-name-save'));
    await waitFor(() => expect(patches.length).toBe(1));

    // Swept over every key of every request this page made, rather than
    // over a list of the two it is expected to send: a body that grew a
    // third key is what this asserts against.
    const keys = patches.flatMap((patch) => Object.keys(patch.body));
    expect(
      keys.filter((key) => /secret|password|username|hash/i.test(key)),
    ).toEqual([]);
    expect(keys.length).toBeGreaterThan(0);
  });
});
