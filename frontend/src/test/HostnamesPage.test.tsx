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
import { composeHostname } from '../tenant/HostnameList';
import { DEVICE_PERMISSION, type Device } from '../api/devices';
import { DOMAIN_PERMISSION, type Domain } from '../api/domains';
import {
  HOSTNAME_PERMISSION,
  type Hostname,
  type HostnamePublishing,
} from '../api/hostnames';
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
      // `NameModal` reads a name's publishing configuration before it can
      // seed the TTL. Unserved, the modal never leaves "Loading…" — and
      // that reads in a failure as "the modal never opened".
      if (/\/atrium_ddns\/hostnames\/\d+\/backends$/.test(url) && method === 'GET') {
        return json(publishingPayload);
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

/** What `GET /hostnames/:id/backends` answers. An empty provider list
 *  means *this name follows its zone* — the state a name is created in
 *  and the one most rows are in. */
let publishingPayload: HostnamePublishing = {
  hostname_id: 100,
  name: `home.${ZONE}`,
  domain_id: 1,
  domain_name: ZONE,
  device_id: 7,
  // Inheriting: nothing is pinned, so the name follows its zone. The
  // state a name is created in and the one most rows are in.
  inherits_backends: true,
  ttl: null,
  default_ttl: 60,
  ttl_min: 30,
  ttl_max: 86400,
  backends: [],
  publishes_to: [],
};

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

/** Open an existing name's modal. The row's name is the control: the
 *  gear and the row dropdown are gone, and one modal holds every setting
 *  the name has. */
async function openTheName(name: string) {
  fireEvent.click(screen.getByTestId(`hostname-${name}-link`));
  await waitFor(() =>
    expect(screen.getByTestId('name-modal-body')).toBeInTheDocument(),
  );
}

async function openTheForm() {
  fireEvent.click(screen.getByTestId('add-hostname'));
  await waitFor(() =>
    expect(screen.getByTestId('hostname-name')).toBeInTheDocument(),
  );
}

/** The composed string, read off the preview as its own node.
 *
 *  Deliberately not a substring match on the sentence. `home.example.net`
 *  is a substring of `home.example.net..example.net`, so
 *  `toContain(expected)` would pass on the doubled composition — an
 *  assertion on the report rather than on the thing reported.
 */
function willSend(): string | null {
  const preview = screen.queryByTestId('hostname-will-send');
  return preview === null ? null : preview.textContent;
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
    // The model's own state, rendered as a value. A blank cell would
    // read as missing data. It is a cell rather than a dropdown now —
    // the row shows, the modal edits.
    expect(screen.getByTestId(`assigned-spare.${ZONE}`)).toHaveTextContent(
      'Not assigned',
    );
    expect(
      screen.queryByTestId(`assign-spare.${ZONE}`),
      'the row still carries a control that mutates data',
    ).not.toBeInTheDocument();
  });

  test('reassigning sends the new device id', async () => {
    devicesPayload = [device(), device({ id: 8, name: 'shed-router' })];
    await mount(OPERATOR);
    await openTheName(`home.${ZONE}`);
    await choose('hostname-device', 'shed-router');
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() => expect(sent.length).toBeGreaterThan(0));
    expect(sent[0].method).toBe('PATCH');
    expect(sent[0].url).toContain('/atrium_ddns/hostnames/100');
    // Only the key that changed. The modal holds the name, the zone, the
    // device and the TTL, and sending all four on every save would make
    // a device change able to rename the row it was opened from.
    expect(sent[0].body).toEqual({ device_id: 8 });
  });

  test('unassigning sends an explicit null, not an omitted key', async () => {
    // `null` and *absent* are different requests, and the endpoint reads
    // them differently. A body of `{}` would leave the device attached.
    await mount(OPERATOR);
    await openTheName(`home.${ZONE}`);
    await choose('hostname-device', 'Not assigned');
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() => expect(sent.length).toBeGreaterThan(0));
    expect(sent[0].body).toEqual({ device_id: null });
    expect('device_id' in sent[0].body).toBe(true);
  });
});

describe('creating a name', () => {
  test('sends the composed name, the zone id and an explicit device', async () => {
    // The zone is typed once, in the select. What goes in the field is
    // the part in front of it.
    hostnamesPayload = [];
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: '  attic  ' },
    });
    fireEvent.click(screen.getByTestId('name-submit'));

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
    // like what is on screen. Since #90 the same line carries the other
    // half: whether the zone was appended or was already there.
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: '  attic ' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('hostname-will-send')).toBeInTheDocument(),
    );
    expect(willSend()).toBe(`attic.${ZONE}`);
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
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.device_id).toBe(7);
  });
});

/** #90 — the zone is a suffix, not a retype.
 *
 * Driven through the DOM rather than against `composeHostname` alone.
 * A table over a pure function is a table over a function that might
 * have no caller; typing into the field and reading the preview is the
 * same table asserted about the thing the operator actually touches.
 * The pure-function row underneath then only has to prove the two
 * agree.
 */
describe('the zone is a suffix, not a retype', () => {
  /** what is typed → what leaves the browser, with the zone
   *  `example.net` selected. `null` means the preview is not rendered
   *  at all, which is a different state from an empty string. */
  const TABLE: [label: string, typed: string, composed: string | null][] = [
    ['a bare label gets the zone appended', 'home', `home.${ZONE}`],
    [
      'a pasted FQDN is not suffixed twice',
      `home.${ZONE}`,
      `home.${ZONE}`,
    ],
    [
      'a pasted FQDN in a different case is still recognised',
      `home.${ZONE.toUpperCase()}`,
      // Sent as typed. The server lower-cases on the way in; the
      // browser does not, because "what you typed" is what the preview
      // has to be able to show.
      `home.${ZONE.toUpperCase()}`,
    ],
    [
      'a trailing dot is not special-cased, and the preview says so',
      `home.${ZONE}.`,
      // Deliberate, and pinned so a later "fix" is a decision. A
      // trailing dot marks the root — a fact about the label rule,
      // which lives on the server. `zone_contains` answers False for
      // `foo.example.com.`, so a browser that quietly dropped the dot
      // would be accepting bytes the server refuses.
      `home.${ZONE}..${ZONE}`,
    ],
    ['trailing whitespace is trimmed, then composed', '  home  ', `home.${ZONE}`],
    [
      'a paste with trailing whitespace is trimmed, then recognised',
      `  home.${ZONE}  `,
      `home.${ZONE}`,
    ],
    [
      'the zone in the middle is not the zone at the end',
      // The case the naive `includes()` gets wrong. This name contains
      // `example.net` and does not end with it, so the suffix is
      // appended — anything else would send a name outside the zone.
      `${ZONE}.staging`,
      `${ZONE}.staging.${ZONE}`,
    ],
    ['the apex is left alone', ZONE, ZONE],
    ['an empty field composes to nothing, not to the zone', '', null],
    ['whitespace only is also nothing', '   ', null],
  ];

  test.each(TABLE)('%s', async (_label, typed, composed) => {
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: typed },
    });
    await waitFor(() => expect(willSend()).toBe(composed));
    // …and the pure composer agrees with what the form rendered, so the
    // table below can be read as being about either.
    expect(composeHostname(typed, ZONE)).toBe(composed ?? '');
  });

  test('the table is not vacuous — composition changed something', () => {
    // Every row above could pass against `composeHostname = (s) => s`
    // if the table happened to contain only already-suffixed names.
    const changed = TABLE.filter(
      ([, typed, composed]) => composed !== null && composed !== typed,
    );
    expect(changed.length).toBeGreaterThan(3);
    // …and the identity rows are real too, or "not suffixed twice" is
    // being asserted by a table with no paste in it.
    const unchanged = TABLE.filter(
      ([, typed, composed]) => composed !== null && composed === typed,
    );
    expect(unchanged.length).toBeGreaterThan(1);
  });

  test('the composed name follows the zone select', async () => {
    domainsPayload = [domain(), domain({ id: 2, name: 'example.org' })];
    await mount(OPERATOR);
    await openTheForm();
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: 'attic' },
    });
    // The suffix used to be rendered inside the field, as fixed text. It
    // was a restatement of the select directly beside it, and the
    // operator asked for it gone. What promises the zone now is the
    // `will send:` line — the composed string, which is the one that
    // actually leaves the browser.
    expect(screen.queryByTestId('hostname-suffix')).not.toBeInTheDocument();
    await choose('hostname-zone', ZONE);
    await waitFor(() => expect(willSend()).toBe(`attic.${ZONE}`));
    // …and it moves with the select, which is the property the fixed
    // suffix existed to provide.
    await choose('hostname-zone', 'example.org');
    await waitFor(() => expect(willSend()).toBe('attic.example.org'));
  });

  test('what is previewed is what is posted, byte for byte', async () => {
    // The preview and the request body are two renderings of one
    // string. Asserting only the body would let the preview drift into
    // decoration; asserting only the preview would let it lie.
    hostnamesPayload = [];
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: `  attic.${ZONE.toUpperCase()}  ` },
    });
    const previewed = willSend();
    expect(previewed).toBe(`attic.${ZONE.toUpperCase()}`);
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.name).toBe(previewed);
  });
});

describe('the bundle does not second-guess the server about validity', () => {
  test('a name the server will refuse is still sent, and the refusal is shown', async () => {
    // The one assertion this file exists for, and the guard against the
    // suffix composer growing into a validator. `bad_label` carries an
    // underscore, which `_LABEL` in `providers/base.py` rejects — it is
    // the first thing any client-side hostname regex would block, and
    // it is composed and posted unchanged. The browser has no opinion.
    hostnamesPayload = [];
    nextFailure = {
      status: 422,
      detail:
        `'bad_label.${ZONE}' is not a valid hostname. /nic/update answers ` +
        'notfqdn for it, so the row could be created and never updated.',
    };
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: 'bad_label' },
    });
    // Composition ran — and did not gate. The submit button is live.
    expect(willSend()).toBe(`bad_label.${ZONE}`);
    expect(screen.getByTestId('name-submit')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('name-submit'));

    // It was sent — the browser did not decide.
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.name).toBe(`bad_label.${ZONE}`);

    // …and the server's own words are what the user reads, including the
    // wire status. Diagnostics in full.
    await waitFor(() =>
      expect(screen.getByTestId('name-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('name-error').textContent).toContain(
      'notfqdn',
    );
  });

  test('a label the composer cannot help with is still the server’s call', async () => {
    // A leading hyphen is legal to type, illegal as a label, and
    // untouched by composition — the second shape a client-side
    // validator would have caught. `-bad` is refused by `_LABEL`'s
    // `(?!-)` and the refusal arrives from the server, not from here.
    nextFailure = {
      status: 422,
      detail: `'-bad.${ZONE}' is not a valid hostname (notfqdn).`,
    };
    await mount(OPERATOR);
    await openTheForm();
    await choose('hostname-zone', ZONE);
    fireEvent.change(screen.getByTestId('hostname-name'), {
      target: { value: '-bad' },
    });
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body.name).toBe(`-bad.${ZONE}`);
    await waitFor(() =>
      expect(screen.getByTestId('name-error')).toBeInTheDocument(),
    );
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
      target: { value: 'home' },
    });
    fireEvent.click(screen.getByTestId('name-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('name-error')).toBeInTheDocument(),
    );
    const text = screen.getByTestId('name-error').textContent ?? '';
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

  test('exactly one module in the bundle decides that the zone is already there', async () => {
    // The TypeScript analogue of `router.zone_contains is
    // providers_base.zone_contains`. There is no function identity to
    // assert across a language boundary, so the property asserted is
    // the one that actually matters: **the suffix decision has one
    // home.** A second copy — in a device form, in a validator helper,
    // in a "tidy" utils module — is the drift the backend paid for
    // once, and it is the thing this sweep exists to fail on.
    const sources = import.meta.glob('../**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    // Tests are not shipped, and every fetch stub in this directory
    // matches a URL with `url.endsWith(...)`. The glob resolves them as
    // `./X.test.tsx` — relative to *this* file — so a `/test/` filter
    // does not see them and the sweep would name nine offenders that
    // are all itself.
    const shipped = Object.entries(sources).filter(
      ([path]) => !/\.test\.tsx?$/.test(path),
    );
    // Vacuity: the glob must have read the bundle, not an empty record.
    expect(shipped.length).toBeGreaterThan(10);
    const suffixDeciders = shipped
      .filter(([, text]) => /\.endsWith\(/.test(text))
      .map(([path]) => path);
    expect(
      suffixDeciders,
      'more than one module tests whether a name already ends with its ' +
        'zone. One composer, one caller — see composeHostname’s note on why ' +
        'this is the assertion that replaces the backend’s identity check.',
    ).toEqual(['../tenant/HostnameList.tsx']);
  });
});
