/** The zone list and the create flow — #88.
 *
 * The assertions this file exists for are the create-flow ones. The
 * defect Part II §8.1 measures is not a missing feature: it is that the
 * create modal offered one field and a Create button, so **one click
 * produced a zone that answers `911` for every update under it** and the
 * list then drew it identically to a working zone.
 *
 * So the tests below are about what leaves the browser and what is on
 * screen afterwards:
 *
 * - one submission carries the zone **and** its first provider, in one
 *   request, so the server can make it one transaction;
 * - "add a provider later" is reachable, is not the default, and states
 *   its consequence before it can be taken;
 * - a zone with no provider is marked, in wire terms.
 *
 * The provider *bindings* moved to `ZoneDetailPage` (§10.2) and their
 * tests moved with them — including the blank-preserves assertion, which
 * is the most important one in this bundle.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DomainsPage } from '../DomainsPage';
import {
  DOMAIN_PERMISSION,
  type Domain,
  type DomainBackend,
  type Provider,
} from '../api/domains';
import { WIRE_CONSEQUENCE } from '../tenant/ZoneStatus';
import { zoneHref } from '../paths';
import { queryClient } from '../queryClient';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [DOMAIN_PERMISSION],
  impersonating_from: null,
};

const OUTSIDER: UserContext = {
  id: 2,
  email: 'outsider@example.com',
  full_name: 'Outsider',
  is_active: true,
  roles: [],
  permissions: ['atrium_ddns.device.manage'],
  impersonating_from: null,
};

const PROVIDERS: Provider[] = [
  {
    service: 'route53',
    credential_keys: ['aws_access_key_id', 'aws_secret_access_key'],
  },
  { service: 'hetzner', credential_keys: ['hetzner_api_token'] },
];

function backend(overrides: Partial<DomainBackend> = {}): DomainBackend {
  return {
    id: 10,
    domain_id: 1,
    backend_type: 'route53',
    config: { ttl: 60 },
    credentials_set: true,
    known_service: true,
    credential_keys: ['aws_access_key_id', 'aws_secret_access_key'],
    ...overrides,
  };
}

function domain(overrides: Partial<Domain> = {}): Domain {
  return {
    id: 1,
    name: 'example.invalid',
    created_at: '2026-08-15T10:00:00Z',
    backends: [backend()],
    hostname_count: 3,
    ...overrides,
  };
}

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let domainsPayload: Domain[] = [];
let domainFetches = 0;
/** Every non-GET request the page made, in order. */
let sent: { url: string; method: string; body: unknown }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
  domainFetches = 0;
  sent = [];
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
      }
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return json(currentMe);
      }
      if (url.endsWith('/atrium_ddns/providers')) {
        return json({ providers: PROVIDERS });
      }
      if (url.endsWith('/atrium_ddns/domains') && method === 'GET') {
        domainFetches += 1;
        return json(domainsPayload);
      }
      return json({});
    }),
  );
}

beforeEach(() => {
  stubFetch();
  domainsPayload = [domain()];
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
  renderWithAtrium(<DomainsPage />);
  // `usePerm` answers false while `me` is in flight, so the refusal is
  // also the pre-resolution state and an "either marker" wait settles
  // on it for a permitted user. Wait for the one this user ends on.
  const expected = user?.permissions.includes(DOMAIN_PERMISSION)
    ? 'add-domain'
    : 'domains-refused';
  await waitFor(() =>
    expect(screen.getByTestId(expected)).toBeInTheDocument(),
  );
}

describe('the permission gate', () => {
  test('a non-holder sees a refusal, not an empty list, and fires no request', async () => {
    await mount(OUTSIDER);
    await waitFor(() =>
      expect(screen.getByTestId('domains-refused')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('domains-empty')).not.toBeInTheDocument();
    expect(domainFetches).toBe(0);
  });

  test('a holder sees their zones', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('domain-example.invalid')).toBeInTheDocument(),
    );
  });
});

describe('a zone with no provider is the exceptional row', () => {
  test('it is marked diverged, in the operator’s terms and not the protocol’s', async () => {
    domainsPayload = [domain({ backends: [] })];
    await mount(OPERATOR);
    const row = await screen.findByTestId('domain-example.invalid');

    // The first of the three channels (§1.2 Rule 3) — the treatment the
    // stylesheet keys `--ddns-diverge` off. An attribute rather than a
    // class, so the component does not decide which class means what.
    expect(row).toHaveAttribute('data-diverged', 'true');
    // The second and third: the glyph and the word.
    expect(screen.getByTestId('nowhere-example.invalid')).toHaveTextContent(
      'publishes nowhere',
    );
    expect(screen.getByTestId('nowhere-why-example.invalid')).toHaveTextContent(
      WIRE_CONSEQUENCE,
    );
    // …and the wire fact is stated. `911` is what a router actually
    // receives, and it is the reason this row is marked at all.
    expect(row).toHaveTextContent(/911/);
    // Never the protocol's noun. §10.1: "The operator does not own a
    // backend; they own a zone that does or does not work."
    expect(row.textContent).not.toMatch(/backend/i);
  });

  test('a zone that does publish is not marked — agreement has no colour', async () => {
    // §1.2 Rule 1, and the reason it matters here: if a healthy zone
    // carried the treatment too, the mark would be on every row and
    // would mean nothing on any of them.
    await mount(OPERATOR);
    const row = await screen.findByTestId('domain-example.invalid');
    expect(row).toHaveAttribute('data-diverged', 'false');
    expect(
      screen.queryByTestId('nowhere-example.invalid'),
    ).not.toBeInTheDocument();
    expect(row.textContent).not.toMatch(/911/);
  });

  test('the row links to the zone’s own route', async () => {
    // §12's first argument for a route over a drawer: it is linkable.
    await mount(OPERATOR);
    const link = await screen.findByTestId('open-domain-example.invalid');
    expect(link).toHaveAttribute('href', zoneHref(1));
    expect(link).toHaveAttribute('href', '/atrium-ddns/zones/1');
  });
});

describe('creating a zone', () => {
  async function openCreate() {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-name')).toBeInTheDocument(),
    );
  }

  test('the provider is in the form, and it is not optional-looking', async () => {
    await openCreate();
    // The zone field and the provider select are in the same submission.
    expect(screen.getByTestId('zone-name')).toBeInTheDocument();
    expect(screen.getByTestId('backend-service')).toBeInTheDocument();
    // …and the credential fields are `BackendForm`'s, derived from
    // `GET /providers`. A field list retyped into a create-only fork is
    // the identical defect one release later, and this is the assertion
    // that the fork does not exist.
    fireEvent.click(screen.getByTestId('credential-mode-replace'));
    await waitFor(() =>
      expect(
        screen.getByTestId('credential-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('credential-aws_secret_access_key'),
    ).toBeInTheDocument();
  });

  test('one submission sends the zone and its first provider in one request', async () => {
    await openCreate();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: '  new.example.invalid  ' },
    });
    fireEvent.click(screen.getByTestId('credential-mode-replace'));
    await waitFor(() =>
      expect(
        screen.getByTestId('credential-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('credential-aws_access_key_id'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByTestId('credential-aws_secret_access_key'), {
      target: { value: 'sekrit' },
    });
    fireEvent.click(screen.getByTestId('backend-submit'));

    // **One** request. Two would be able to half-succeed — the zone
    // lands, the credential is refused — and leave behind exactly the
    // zero-provider zone this whole issue is about, while telling the
    // operator their submission failed.
    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('POST');
    expect(sent[0].url).toContain('/atrium_ddns/domains');
    expect(sent[0].url).not.toContain('/backends');
    expect(sent[0].body).toEqual({
      // Trimmed by the form, visibly: the API does not strip.
      name: 'new.example.invalid',
      backend: {
        backend_type: 'route53',
        config: {},
        credentials: {
          aws_access_key_id: 'AKIAEXAMPLE',
          aws_secret_access_key: 'sekrit',
        },
      },
    });
  });

  test('the submit is unavailable until the zone has a name', async () => {
    await openCreate();
    expect(screen.getByTestId('backend-submit')).toBeDisabled();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'new.example.invalid' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('backend-submit')).not.toBeDisabled(),
    );
  });

  test('a half-filled credential is refused in the form, and no zone is created', async () => {
    // The atomicity argument from the browser's side: the request never
    // leaves, so there is no zone to clean up.
    await openCreate();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'new.example.invalid' },
    });
    fireEvent.click(screen.getByTestId('credential-mode-replace'));
    await waitFor(() =>
      expect(
        screen.getByTestId('credential-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('credential-aws_access_key_id'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.click(screen.getByTestId('backend-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('backend-form-problem')).toHaveTextContent(
        'aws_secret_access_key',
      ),
    );
    expect(sent).toEqual([]);
    expect(
      screen.getByTestId('backend-form-problem').textContent,
    ).not.toContain('AKIAEXAMPLE');
  });
});

describe('“add a provider later” — a link, not a checkbox, not the default', () => {
  test('there is no checkbox, and nothing is pre-selected for the user', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-later-link')).toBeInTheDocument(),
    );
    // A `button` rendered as a link, never an `input[type=checkbox]`:
    // a checkbox sits in the form's reading order as one more option and
    // can be left in either state by accident.
    expect(screen.getByTestId('zone-later-link').tagName).toBe('BUTTON');
    expect(
      document.querySelectorAll('input[type="checkbox"]'),
    ).toHaveLength(0);
    // Not the default: the consequence and its confirm button are not
    // even rendered until the link is taken.
    expect(screen.queryByTestId('zone-later-warning')).not.toBeInTheDocument();
    expect(screen.queryByTestId('zone-later-submit')).not.toBeInTheDocument();
  });

  test('taking it states the wire consequence before it can be confirmed', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-later-link')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('zone-later-link'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-later-warning')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('zone-later-warning')).toHaveTextContent(
      WIRE_CONSEQUENCE,
    );
    expect(screen.getByTestId('zone-later-warning')).toHaveTextContent(/911/);
  });

  test('confirming sends an explicit null backend, not an omitted key', async () => {
    // `null` and "absent" would both create a zero-provider zone, and
    // only one of them says on the wire that it was asked for. The audit
    // row records which shape the call was.
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-name')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'staged.example.invalid' },
    });
    fireEvent.click(screen.getByTestId('zone-later-link'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-later-submit')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('zone-later-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].body).toEqual({
      name: 'staged.example.invalid',
      backend: null,
    });
    expect(
      Object.keys(sent[0].body as Record<string, unknown>),
    ).toContain('backend');
  });
});
