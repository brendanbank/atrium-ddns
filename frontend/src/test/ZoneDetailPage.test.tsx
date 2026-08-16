/** `/atrium-ddns/zones/:id` — #88, design Part II §10.2 and §12.
 *
 * Two groups of assertion live here, and they arrived from different
 * places.
 *
 * The first is the route's own: it reads its id off the pathname
 * (`useParams` cannot reach a second React root), it distinguishes *no
 * such zone* from *you own no zones*, and it lists the providers inside
 * the zone rather than three clicks away on a shared list.
 *
 * The second **moved here from `DomainsPage.test.tsx`** when the
 * bindings moved, and it is the one this bundle would least like to
 * lose: `editing an unrelated field sends preserve, not a blank` drives
 * the real form and reads the request body that actually left the
 * browser. `credentials.test.ts` proves `buildCredentialsPayload` is
 * right; this proves the *form* calls it with the mode the user is
 * looking at. A pure function and its caller share an author, so testing
 * the function alone leaves the wiring unmeasured — and the wiring is
 * where "editing a TTL blanked my Route53 key" actually happens.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';
import { __resetAtriumLocationCacheForTests } from '@brendanbank/atrium-host-bundle-utils/react';

import { ZoneDetailPage } from '../ZoneDetailPage';
import {
  DOMAIN_PERMISSION,
  type Domain,
  type DomainBackend,
  type Provider,
} from '../api/domains';
import { HOSTNAME_PERMISSION, type Hostname } from '../api/hostnames';
import { WIRE_CONSEQUENCE } from '../tenant/ZoneStatus';
import { zoneHref } from '../paths';
import { queryClient } from '../queryClient';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [DOMAIN_PERMISSION, HOSTNAME_PERMISSION],
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
    hostname_count: 2,
    ...overrides,
  };
}

const HOSTNAMES: Hostname[] = [
  {
    id: 100,
    name: 'home.example.invalid',
    domain_id: 1,
    domain_name: 'example.invalid',
    device_id: 5,
    device_name: 'router-a',
    created_at: '2026-08-15T10:00:00Z',
    last_ip_v4: '192.0.2.10',
    last_ip_v6: null,
    last_updated_at: '2026-08-15T11:00:00Z',
  },
  {
    id: 101,
    // In another zone. The filter is the assertion.
    name: 'box.other.invalid',
    domain_id: 2,
    domain_name: 'other.invalid',
    device_id: null,
    device_name: null,
    created_at: '2026-08-15T10:00:00Z',
    last_ip_v4: null,
    last_ip_v6: null,
    last_updated_at: null,
  },
];

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let domainsPayload: Domain[] = [];
let sent: { url: string; method: string; body: unknown }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
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
        return json(domainsPayload);
      }
      if (url.endsWith('/atrium_ddns/hostnames') && method === 'GET') {
        return json(HOSTNAMES);
      }
      if (url.includes('/atrium_ddns/backends/')) {
        return json(backend());
      }
      return json({});
    }),
  );
}

/** Put the browser on the route under test.
 *
 * The page reads its id off `window.location.pathname` through
 * `useAtriumLocation`, which caches its snapshot at module level — so
 * the cache is reset here as well as the URL. Without the reset the
 * second test in a file reads the first test's pathname, which is the
 * kind of cross-test leak that looks like a component bug.
 */
function atPath(pathname: string) {
  window.history.replaceState({}, '', pathname);
  __resetAtriumLocationCacheForTests();
}

beforeEach(() => {
  stubFetch();
  domainsPayload = [domain()];
  atPath(zoneHref(1));
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  handles?.cleanup();
  vi.unstubAllGlobals();
  currentMe = null;
  atPath('/');
});

async function mount(user: UserContext | null, marker: string) {
  currentMe = user;
  handles = mockAtriumRegistry({ me: user });
  renderWithAtrium(<ZoneDetailPage />);
  await waitFor(() => expect(screen.getByTestId(marker)).toBeInTheDocument());
}

describe('the five states, kept five', () => {
  test('a non-holder sees a refusal, not an empty page', async () => {
    await mount(OUTSIDER, 'zone-refused');
    expect(screen.queryByTestId('zone-missing')).not.toBeInTheDocument();
  });

  test('an id that is not in this tenant’s zones is “no such zone”, not “no zones”', async () => {
    // The distinction the fifth state exists for. Rendering this as the
    // list's empty state would state a fact about the account that is
    // not true.
    atPath(zoneHref(999));
    await mount(OPERATOR, 'zone-missing');
    expect(screen.getByTestId('zone-missing')).toHaveTextContent('999');
    expect(screen.getByTestId('zone-missing-back')).toHaveAttribute(
      'href',
      '/atrium-ddns/domains',
    );
  });

  test('a pathname with no id at all says so about the URL, not about the data', async () => {
    atPath('/atrium-ddns/zones/not-a-number');
    await mount(OPERATOR, 'zone-bad-url');
    expect(screen.queryByTestId('zone-missing')).not.toBeInTheDocument();
  });

  test('the zone renders, with its providers inside it', async () => {
    await mount(OPERATOR, 'zone-example.invalid');
    // §10.2: providers are listed *inside* the zone. The previous build
    // nested them in an accordion on a shared list page — the same
    // information one level too deep and three clicks from the thing it
    // describes.
    expect(screen.getByTestId('backend-route53')).toBeInTheDocument();
    expect(screen.getByTestId('credentials-10')).toHaveTextContent(
      'credential stored',
    );
    // No mask, no length, no prefix.
    expect(document.body.textContent).not.toMatch(/[•*]{3,}/);
    expect(document.body.textContent).not.toMatch(/AKIA/);
  });
});

describe('a zero-provider zone, on its own page', () => {
  test('is diverged here too, with the same words as the list', async () => {
    domainsPayload = [domain({ backends: [] })];
    await mount(OPERATOR, 'zone-example.invalid');
    expect(screen.getByTestId('zone-example.invalid')).toHaveAttribute(
      'data-diverged',
      'true',
    );
    expect(screen.getByTestId('zone-nowhere')).toHaveTextContent(
      'publishes nowhere',
    );
    // The same constant the list renders. Two surfaces, one sentence —
    // a second copy would be a second sentence the moment one is edited.
    expect(screen.getByTestId('zone-nowhere-why')).toHaveTextContent(
      WIRE_CONSEQUENCE,
    );
    expect(screen.getByTestId('zone-no-providers')).toBeInTheDocument();
  });

  test('a publishing zone carries no mark', async () => {
    await mount(OPERATOR, 'zone-example.invalid');
    expect(screen.getByTestId('zone-example.invalid')).toHaveAttribute(
      'data-diverged',
      'false',
    );
    expect(screen.queryByTestId('zone-nowhere')).not.toBeInTheDocument();
  });
});

describe('the names in this zone', () => {
  test('are filtered to this zone, and the others are not shown', async () => {
    await mount(OPERATOR, 'zone-example.invalid');
    await waitFor(() =>
      expect(
        screen.getByTestId('zone-name-home.example.invalid'),
      ).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId('zone-name-box.other.invalid'),
    ).not.toBeInTheDocument();
  });

  test('a caller without the names permission is refused, not shown an empty zone', async () => {
    currentMe = null;
    const zonesOnly: UserContext = {
      ...OPERATOR,
      permissions: [DOMAIN_PERMISSION],
    };
    await mount(zonesOnly, 'zone-example.invalid');
    await waitFor(() =>
      expect(screen.getByTestId('zone-names-refused')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('zone-names-empty')).not.toBeInTheDocument();
  });
});

describe('the credential form — moved here with the bindings', () => {
  test('defaults to keeping a stored credential', async () => {
    await mount(OPERATOR, 'edit-backend-10');
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-keep')).toBeChecked(),
    );
    // …and the fields are not even rendered, so there is nothing to
    // accidentally type a partial value into.
    expect(
      screen.queryByTestId('credential-aws_access_key_id'),
    ).not.toBeInTheDocument();
  });

  test('editing an unrelated field sends preserve, not a blank', async () => {
    // The assertion this bundle would least like to lose. Read off the
    // request body that actually left the browser, not off the builder.
    await mount(OPERATOR, 'edit-backend-10');
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('backend-config')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('backend-config'), {
      target: { value: '{"ttl": 300}' },
    });
    fireEvent.click(screen.getByTestId('backend-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    const request = sent[0];
    expect(request.method).toBe('PATCH');
    expect(request.body).toEqual({
      config: { ttl: 300 },
      // `""`, the preserve sentinel — not `null` (which would clear it)
      // and not an object (which would replace it with whatever the
      // untouched boxes held).
      credentials: '',
    });
  });

  test('clearing is an explicit choice and sends null', async () => {
    await mount(OPERATOR, 'edit-backend-10');
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-clear')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('credential-mode-clear'));
    fireEvent.click(screen.getByTestId('backend-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect((sent[0].body as { credentials: unknown }).credentials).toBeNull();
  });

  test('adding a provider from here posts to this zone', async () => {
    domainsPayload = [domain({ backends: [] })];
    await mount(OPERATOR, 'zone-add-backend');
    fireEvent.click(screen.getByTestId('zone-add-backend'));
    await waitFor(() =>
      expect(screen.getByTestId('backend-service')).toBeInTheDocument(),
    );
    // A new binding has nothing stored, so `replace` is the only
    // reachable mode and both fields are required — the form refuses a
    // half-filled credential rather than sending one.
    fireEvent.change(screen.getByTestId('credential-aws_access_key_id'), {
      target: { value: 'AKIAEXAMPLE' },
    });
    fireEvent.change(screen.getByTestId('credential-aws_secret_access_key'), {
      target: { value: 'sekrit' },
    });
    fireEvent.click(screen.getByTestId('backend-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('POST');
    expect(sent[0].url).toContain('/atrium_ddns/domains/1/backends');
  });
});

describe('rename and delete live on the zone, not on the list', () => {
  test('the rename form sends a PATCH, and nothing else', async () => {
    // `PATCH`, not `PUT`. The server leaves `PUT` at 405 deliberately,
    // so a client that sent one would get a method-not-allowed that
    // reads to an operator as "renaming does not work".
    await mount(OPERATOR, 'zone-rename');
    fireEvent.click(screen.getByTestId('zone-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-name')).toBeInTheDocument(),
    );
    // The box opens on the current name, so a rename is an edit rather
    // than a re-type — and the submit is disabled until it changes.
    expect(screen.getByTestId('rename-zone-name')).toHaveValue(
      'example.invalid',
    );
    expect(screen.getByTestId('rename-zone-submit')).toBeDisabled();

    fireEvent.change(screen.getByTestId('rename-zone-name'), {
      target: { value: '  renamed.invalid  ' },
    });
    fireEvent.click(screen.getByTestId('rename-zone-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('PATCH');
    expect(sent[0].url).toContain('/atrium_ddns/domains/1');
    expect(sent[0].body).toEqual({ name: 'renamed.invalid' });
  });

  test('the rename warns about the names before the server has to refuse', async () => {
    // The count is the server's own `hostname_count` for this zone, so
    // the warning cannot claim a number the page does not show.
    await mount(OPERATOR, 'zone-rename');
    fireEvent.click(screen.getByTestId('zone-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-warning')).toHaveTextContent(
        '2 names',
      ),
    );
    expect(screen.getByTestId('rename-zone-warning')).toHaveTextContent(
      'refused rather than rewriting them',
    );
  });

  test("the server's refusal is shown verbatim, counts and all", async () => {
    // The 409 detail carries the number of names that would be orphaned
    // and a sample of them. Rewording it here would throw away the only
    // thing that makes the refusal actionable — and this bundle has no
    // second copy of `zone_contains` with which to predict it.
    const detail =
      "renaming 'example.invalid' to 'renamed.invalid' would leave 2 of 2 " +
      "hostnames outside the zone: 'home.example.invalid'";
    currentMe = OPERATOR;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        if (url.endsWith('/users/me/context')) return json(OPERATOR);
        if (url.endsWith('/atrium_ddns/providers'))
          return json({ providers: PROVIDERS });
        if (url.endsWith('/atrium_ddns/domains') && method === 'GET')
          return json([domain()]);
        if (url.endsWith('/atrium_ddns/hostnames') && method === 'GET')
          return json(HOSTNAMES);
        if (method === 'PATCH') return json({ detail }, 409);
        return json({});
      }),
    );
    await mount(OPERATOR, 'zone-rename');
    fireEvent.click(screen.getByTestId('zone-rename'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-name')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('rename-zone-name'), {
      target: { value: 'renamed.invalid' },
    });
    fireEvent.click(screen.getByTestId('rename-zone-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('zone-action-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('zone-action-error')).toHaveTextContent(
      '2 of 2 hostnames outside the zone',
    );
    expect(screen.getByTestId('zone-action-error')).toHaveTextContent(
      'home.example.invalid',
    );
  });

  test('deleting says what it destroys, credentials included', async () => {
    await mount(OPERATOR, 'zone-delete');
    fireEvent.click(screen.getByTestId('zone-delete'));
    await waitFor(() =>
      expect(screen.getByTestId('delete-zone-warning')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('delete-zone-warning')).toHaveTextContent(
      /credentials included/i,
    );
    expect(screen.getByTestId('delete-zone-warning')).toHaveTextContent(
      /2 names/,
    );
  });
});
