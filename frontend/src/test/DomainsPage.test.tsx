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
import { zoneHrefParam } from '../paths';
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
    credential_labels: {},
    setting_fields: [],
  },
  {
    service: 'hetzner',
    credential_keys: ['hetzner_api_token'],
    credential_labels: {},
    setting_fields: [],
  },
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

  test('the row links to the zone, and the link is the modal', async () => {
    // Still linkable, which was §12's first argument — the address just
    // carries the zone as a query parameter rather than a path segment,
    // because a second route unmounts the host root under the portalled
    // modal. Asserted twice on purpose: once against the constant every
    // link is built from, once against the literal, so a change to
    // `zoneHrefParam` cannot move every link and every assertion together.
    await mount(OPERATOR);
    const link = await screen.findByTestId('open-domain-example.invalid');
    expect(link).toHaveAttribute('href', zoneHrefParam(1));
    expect(link).toHaveAttribute('href', '/atrium-ddns/domains?zone=1');
  });
});

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

describe('creating a zone', () => {
  /** Open the modal and pick the provider. **Nothing is preselected**:
   *  a zone publishes through exactly one provider and that is not a
   *  choice to make on the operator's behalf, so `zone-provider` starts
   *  empty and the credential fields do not exist until it is set. */
  async function openCreate(service = 'route53') {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-name')).toBeInTheDocument(),
    );
    await choose('zone-provider', service);
  }

  test('the provider is in the form, and it is not optional-looking', async () => {
    await openCreate();
    // The zone field and the provider select are in the same submission.
    expect(screen.getByTestId('zone-name')).toBeInTheDocument();
    expect(screen.getByTestId('zone-provider')).toBeInTheDocument();
    // …and the credential fields are `BackendForm`'s, derived from
    // `GET /providers`. A field list retyped into a create-only fork is
    // the identical defect one release later, and this is the assertion
    // that the fork does not exist.
    await waitFor(() =>
      expect(
        screen.getByTestId('zone-credential-field-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByTestId('zone-credential-field-aws_secret_access_key'),
    ).toBeInTheDocument();
  });

  test('one submission sends the zone and its first provider in one request', async () => {
    await openCreate();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: '  new.example.invalid  ' },
    });
    await waitFor(() =>
      expect(
        screen.getByTestId('zone-credential-field-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('zone-credential-field-aws_access_key_id'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByTestId('zone-credential-field-aws_secret_access_key'), {
      target: { value: 'sekrit' },
    });
    fireEvent.click(screen.getByTestId('zone-submit'));

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
        config: { ttl: 60 },
        credentials: {
          aws_access_key_id: 'AKIAEXAMPLE',
          aws_secret_access_key: 'sekrit',
        },
      },
    });
  });

  test('the submit is unavailable until the zone has a name', async () => {
    await openCreate();
    expect(screen.getByTestId('zone-submit')).toBeDisabled();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'new.example.invalid' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('zone-submit')).not.toBeDisabled(),
    );
  });

  test('a half-filled credential is refused in the form, and no zone is created', async () => {
    // The atomicity argument from the browser's side: the request never
    // leaves, so there is no zone to clean up.
    await openCreate();
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'new.example.invalid' },
    });
    await waitFor(() =>
      expect(
        screen.getByTestId('zone-credential-field-aws_access_key_id'),
      ).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('zone-credential-field-aws_access_key_id'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.click(screen.getByTestId('zone-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('zone-modal-error')).toHaveTextContent(
        'aws_secret_access_key',
      ),
    );
    expect(sent).toEqual([]);
    expect(
      screen.getByTestId('zone-modal-error').textContent,
    ).not.toContain('AKIAEXAMPLE');
  });
});

describe('a zone cannot be created without a provider', () => {
  // The old create form offered "add a provider later" — a link that
  // posted `backend: null` after stating the wire consequence. It is
  // gone, and its absence is the assertion: a zone publishes through
  // exactly one provider (split-horizon is two zones), so the escape
  // hatch was an affordance for producing the one state the whole
  // surface warns about.
  //
  // The zero-provider zone is still reachable by legacy import, and it
  // is still marked — `a zone with no provider is the exceptional row`
  // above covers that, so removing the way to *make* one did not remove
  // the way to *see* one.
  test('there is no escape hatch, and no checkbox standing in for one', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-name')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('zone-later-link')).not.toBeInTheDocument();
    expect(screen.queryByTestId('zone-later-submit')).not.toBeInTheDocument();
    // Not replaced by a checkbox either: a checkbox sits in the reading
    // order as one more option and can be left in either state by
    // accident, which is the argument the link was chosen over.
    expect(document.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
  });

  test('the submit stays unavailable until a provider is chosen', async () => {
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('add-domain'));
    await waitFor(() =>
      expect(screen.getByTestId('zone-name')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('zone-name'), {
      target: { value: 'staged.example.invalid' },
    });
    // A name alone is not enough. Nothing is preselected, so the button
    // cannot be reached by filling in the one field that looks required.
    expect(screen.getByTestId('zone-submit')).toBeDisabled();
    await choose('zone-provider', 'route53');
    await waitFor(() =>
      expect(screen.getByTestId('zone-submit')).not.toBeDisabled(),
    );
    expect(sent).toEqual([]);
  });
});
