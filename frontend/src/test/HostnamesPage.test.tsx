/** Names — the surface #69 added, and the one rule it must not grow.
 *
 * The assertion this file exists for is
 * **`the bundle does not second-guess the server about validity`**: a
 * name the server would refuse is *sent* rather than blocked, and the
 * server's refusal is what the user reads. There is deliberately no
 * client-side hostname regex, because a third implementation of a rule
 * that `/nic/update` and `POST /hostnames` already share would be the
 * one nobody tests against the wire — and its failure mode is a form
 * that refuses a name the router can update, or accepts one it cannot.
 *
 * The rest is the four-state table every page in this bundle keeps
 * (refused / loading / failed / empty), plus the three-state device
 * column: *not assigned* is a choice, not a blank.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { HostnamesPage } from '../HostnamesPage';
import { DEVICE_PERMISSION, type Device } from '../api/devices';
import { DOMAIN_PERMISSION, type Domain } from '../api/domains';
import { HOSTNAME_PERMISSION, type Hostname } from '../api/hostnames';
import { queryClient } from '../queryClient';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [HOSTNAME_PERMISSION, DOMAIN_PERMISSION, DEVICE_PERMISSION],
  impersonating_from: null,
};

const OUTSIDER: UserContext = {
  id: 2,
  email: 'outsider@example.com',
  full_name: 'Outsider',
  is_active: true,
  roles: [],
  // Holds the neighbouring permissions and not this one, so the refusal
  // below is about `hostname.manage` specifically rather than about
  // being logged out.
  permissions: [DOMAIN_PERMISSION, DEVICE_PERMISSION],
  impersonating_from: null,
};

const ZONE = 'example.net';

function domain(overrides: Partial<Domain> = {}): Domain {
  return {
    id: 1,
    name: ZONE,
    created_at: '2026-08-15T10:00:00Z',
    backends: [],
    hostname_count: 1,
    ...overrides,
  };
}

function device(overrides: Partial<Device> = {}): Device {
  return {
    id: 7,
    name: 'attic-router',
    username: 'ddns-abc123',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-16T09:00:00Z',
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued',
    hostname_count: 1,
    ...overrides,
  };
}

function hostname(overrides: Partial<Hostname> = {}): Hostname {
  return {
    id: 100,
    name: `home.${ZONE}`,
    domain_id: 1,
    domain_name: ZONE,
    device_id: 7,
    device_name: 'attic-router',
    created_at: '2026-08-15T11:00:00Z',
    last_ip_v4: '203.0.113.10',
    last_ip_v6: null,
    last_updated_at: '2026-08-16T08:00:00Z',
    ...overrides,
  };
}

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let hostnamesPayload: Hostname[] = [];
let domainsPayload: Domain[] = [];
let devicesPayload: Device[] = [];
let hostnameFetches = 0;
/** Every non-GET request the page made, in order — the instrument for
 *  "what actually left the browser". */
let sent: { url: string; method: string; body: any }[] = [];
/** When set, the next non-GET answers with this status and body. */
let nextFailure: { status: number; detail: string } | null = null;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
  hostnameFetches = 0;
  sent = [];
  nextFailure = null;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (method !== 'GET') {
        sent.push({
          url,
          method,
          body: init?.body ? JSON.parse(init.body as string) : undefined,
        });
        if (nextFailure) {
          const failure = nextFailure;
          nextFailure = null;
          return json({ detail: failure.detail }, failure.status);
        }
      }
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return json(currentMe);
      }
      if (url.endsWith('/atrium_ddns/hostnames') && method === 'GET') {
        hostnameFetches += 1;
        return json(hostnamesPayload);
      }
      if (url.endsWith('/atrium_ddns/domains') && method === 'GET') {
        return json(domainsPayload);
      }
      if (url.endsWith('/atrium_ddns/devices') && method === 'GET') {
        return json(devicesPayload);
      }
      return json({});
    }),
  );
}

beforeEach(() => {
  stubFetch();
  hostnamesPayload = [hostname()];
  domainsPayload = [domain()];
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
  renderWithAtrium(<HostnamesPage />);
  // `usePerm` answers false while `me` is in flight, so the refusal is
  // also the pre-resolution state. Wait for the marker this user ends
  // on rather than for "either".
  const expected = user?.permissions.includes(HOSTNAME_PERMISSION)
    ? 'add-hostname'
    : 'hostnames-refused';
  await waitFor(() =>
    expect(screen.getByTestId(expected)).toBeInTheDocument(),
  );
}

async function openTheForm() {
  fireEvent.click(screen.getByTestId('add-hostname'));
  await waitFor(() =>
    expect(screen.getByTestId('hostname-name')).toBeInTheDocument(),
  );
}

/** Mantine's `Select` is not a `<select>`; open it, then click the
 *  option.
 *
 *  Scoped to `role="option"` rather than to the label text. A zone name
 *  appears in the ledger *and* in the dropdown, so `getByText(ZONE)`
 *  finds two nodes and throws — and the throw looks like "the dropdown
 *  never opened", which is the wrong diagnosis entirely.
 */
async function choose(testid: string, label: string) {
  fireEvent.click(screen.getByTestId(testid));
  const options = () =>
    Array.from(document.querySelectorAll('[data-combobox-option]'));
  await waitFor(() => expect(options().length).toBeGreaterThan(0));
  const option = options().find((node) => node.textContent === label);
  expect(
    option,
    `no option labelled ${label}; saw ${options()
      .map((node) => node.textContent)
      .join(' | ')}`,
  ).toBeDefined();
  fireEvent.click(option!);
}

describe('the permission gate', () => {
  test('a non-holder sees a refusal, not an empty list, and fires no request', async () => {
    await mount(OUTSIDER);
    expect(screen.getByTestId('hostnames-refused')).toBeInTheDocument();
    // The four states stay four: a refusal is not an empty list.
    expect(screen.queryByTestId('hostnames-empty')).not.toBeInTheDocument();
    expect(hostnameFetches).toBe(0);
  });

  test('the refusal names the permission that is missing', async () => {
    await mount(OUTSIDER);
    expect(screen.getByTestId('hostnames-refused').textContent).toContain(
      HOSTNAME_PERMISSION,
    );
  });

  test('a holder sees their names', async () => {
    await mount(OPERATOR);
    expect(screen.getByTestId(`hostname-home.${ZONE}`)).toBeInTheDocument();
    expect(hostnameFetches).toBe(1);
  });
});

describe('the four states stay four', () => {
  test('an empty account is empty, not refused and not broken', async () => {
    hostnamesPayload = [];
    await mount(OPERATOR);
    expect(screen.getByTestId('hostnames-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('hostnames-refused')).not.toBeInTheDocument();
    expect(screen.queryByTestId('hostnames-error')).not.toBeInTheDocument();
  });

  test('no zone yet is its own state, and offers the next action', async () => {
    // A name has to live in a zone. Rendering "you have no names" to
    // someone who has no *zone* sends them looking for a button that
    // cannot help them.
    hostnamesPayload = [];
    domainsPayload = [];
    await mount(OPERATOR);
    const message = screen.getByTestId('hostnames-no-zone');
    expect(message.textContent).toContain('Add a zone first');
    // …and the create button is unusable rather than merely unhelpful.
    expect(screen.getByTestId('add-hostname')).toBeDisabled();
  });
});

describe('the device column has three states, not two', () => {
  test('an unassigned name reads as a choice, not a blank', async () => {
    hostnamesPayload = [
      hostname({ id: 101, name: `spare.${ZONE}`, device_id: null, device_name: null }),
    ];
    await mount(OPERATOR);
    const select = screen.getByTestId(`assign-spare.${ZONE}`);
    // `Not assigned yet` — the model's own state, rendered as a value.
    // A blank cell would read as missing data.
    expect((select as HTMLInputElement).value).toBe('Not assigned yet');
  });

  test('reassigning sends the new device id', async () => {
    devicesPayload = [device(), device({ id: 8, name: 'shed-router' })];
    await mount(OPERATOR);
    await choose(`assign-home.${ZONE}`, 'shed-router');
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('PATCH');
    expect(sent[0].url).toContain('/atrium_ddns/hostnames/100');
    expect(sent[0].body).toEqual({ device_id: 8 });
  });

  test('unassigning sends an explicit null, not an omitted key', async () => {
    // `null` and *absent* are different requests, and the endpoint reads
    // them differently. A body of `{}` would leave the device attached.
    await mount(OPERATOR);
    await choose(`assign-home.${ZONE}`, 'Not assigned yet');
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body).toEqual({ device_id: null });
    expect('device_id' in sent[0].body).toBe(true);
  });
});

describe('creating a name', () => {
  test('sends the trimmed name, the zone id and an explicit device', async () => {
    hostnamesPayload = [];
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: `  attic.${ZONE}  ` },
    });
    fireEvent.click(screen.getByTestId('hostname-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('POST');
    expect(sent[0].body).toEqual({
      name: `attic.${ZONE}`,
      domain_id: 1,
      device_id: null,
    });
  });

  test('the form shows the exact string it will send', async () => {
    // The API deliberately does not strip — stripping would make it
    // accept a byte sequence `/nic/update` refuses — so the trim happens
    // here, and it happens *visibly*. Without this line a pasted
    // trailing space produces a refusal about a value that does not look
    // like what is on screen.
    await mount(OPERATOR);
    await openTheForm();
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: `  attic.${ZONE} ` },
    });
    await waitFor(() =>
      expect(screen.getByTestId('hostname-preview')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('hostname-preview').textContent).toContain(
      `attic.${ZONE}`,
    );
  });

  test('a device chosen at creation is sent as its id', async () => {
    hostnamesPayload = [];
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: `attic.${ZONE}` },
    });
    await choose('hostname-device', 'attic-router');
    fireEvent.click(screen.getByTestId('hostname-submit'));
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.device_id).toBe(7);
  });
});

describe('the bundle does not second-guess the server about validity', () => {
  test('a name the server will refuse is still sent, and the refusal is shown', async () => {
    // The one assertion this file exists for. `foo` is a dotless label:
    // it passes the legacy syntax check and is then refused for zone
    // containment, exactly as `/nic/update` answers `nohost`. A client
    // regex would have blocked it here — and would have been a third
    // implementation of a rule two code paths already share.
    hostnamesPayload = [];
    nextFailure = {
      status: 422,
      detail:
        "'foo' is not inside the zone 'example.net'. /nic/update answers " +
        'nohost for a hostname outside its domain’s zone, so the row could ' +
        'be created and never updated.',
    };
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: 'foo' },
    });
    fireEvent.click(screen.getByTestId('hostname-submit'));

    // It was sent — the browser did not decide.
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.name).toBe('foo');

    // …and the server's own words are what the user reads, including the
    // wire status. Diagnostics in full.
    await waitFor(() =>
      expect(screen.getByTestId('hostname-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('hostname-error').textContent).toContain('nohost');
  });

  test('a duplicate is surfaced as the server phrased it, not as a crash', async () => {
    nextFailure = {
      status: 409,
      detail: `the hostname home.${ZONE} is already registered`,
    };
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: `home.${ZONE}` },
    });
    fireEvent.click(screen.getByTestId('hostname-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('hostname-error')).toBeInTheDocument(),
    );
    const text = screen.getByTestId('hostname-error').textContent ?? '';
    expect(text).toContain('409');
    expect(text).toContain('already registered');
  });

  test('there is no hostname pattern anywhere in the bundle source', async () => {
    // Derived rather than asserted by eye: the modules that could hold
    // one are read and swept. A regex added later to "help the user"
    // fails here with the file that grew it.
    const sources = import.meta.glob('../{api,tenant}/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const offenders = Object.entries(sources).filter(
      ([, text]) =>
        /new RegExp\(/.test(text) ||
        // A literal character class over the label alphabet is the shape
        // anyone writing a hostname validator reaches for first.
        /\/\^?\[?a-z.*0-9.*\]\{?/i.test(text),
    );
    expect(
      offenders.map(([path]) => path),
      'a hostname pattern appeared in the bundle. The server decides validity, ' +
        'using the same two functions /nic/update uses; a third copy here would ' +
        'be the one nobody tests against the wire.',
    ).toEqual([]);
  });
});
