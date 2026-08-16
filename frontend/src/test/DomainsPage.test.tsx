/** Zones, provider bindings, and the credential form.
 *
 * The assertion this file exists for is the last one:
 * **`test_editing_an_unrelated_field_does_not_touch_the_credential`**
 * drives the real form — open the editor, change the settings JSON,
 * save — and reads the request body that actually left the browser. The
 * unit test in `credentials.test.ts` proves `buildCredentialsPayload`
 * is right; this proves the *form* calls it, with the mode the user is
 * actually looking at, and that nothing between the two turns `''` into
 * something else.
 *
 * That is the two-instruments rule applied to one rule: a pure function
 * and its caller share an author, so testing the function alone leaves
 * the wiring unmeasured — and the wiring is where "editing a TTL
 * blanked my Route53 key" actually happens.
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
    name: 'example.net',
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
/** Every non-GET request the page made, in order. The instrument for
 *  the blank-preserves assertion. */
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
      if (url.includes('/atrium_ddns/backends/')) {
        return json(backend());
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
      expect(screen.getByTestId('domain-example.net')).toBeInTheDocument(),
    );
  });
});

describe('what a backend row says about its credential', () => {
  test('a stored credential is a word, never a masked value', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('credentials-10')).toHaveTextContent(
        'credential stored',
      ),
    );
    // No mask, no length, no prefix: "a prefix of an API token is still
    // a disclosure and it is the shape people reach for first."
    expect(document.body.textContent).not.toMatch(/[•*]{3,}/);
    expect(document.body.textContent).not.toMatch(/AKIA/);
  });

  test('no credential stored is a different word, not the same one', async () => {
    domainsPayload = [
      domain({ backends: [backend({ credentials_set: false })] }),
    ];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('credentials-10')).toHaveTextContent(
        'no credential stored',
      ),
    );
  });

  test('a service this build cannot resolve is named as such', async () => {
    domainsPayload = [
      domain({
        backends: [
          backend({
            backend_type: 'unknownsvc',
            known_service: false,
            credential_keys: [],
          }),
        ],
      }),
    ];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('unknown-10')).toHaveTextContent(/911/),
    );
  });

  test('a zone with no provider says what that means on the wire', async () => {
    domainsPayload = [domain({ backends: [] })];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('domain-example.net')).toHaveTextContent(
        /updates for this zone answer 911/i,
      ),
    );
  });
});

describe('the credential form', () => {
  test('defaults to keeping a stored credential', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
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
    // The assertion this whole file is for. Read off the request body
    // that actually left the browser, not off the builder — the builder
    // and its caller share an author.
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
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
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-clear')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('credential-mode-clear'));
    fireEvent.click(screen.getByTestId('backend-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect((sent[0].body as { credentials: unknown }).credentials).toBeNull();
  });

  test('replacing sends the object, with fields from the provider', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-replace')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('credential-mode-replace'));

    // The fields come from `GET /providers`, which derives them from
    // `BaseProvider.REQUIRED_CREDENTIALS`. A list typed into the
    // frontend is the identical defect one release later.
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

    await waitFor(() => expect(sent.length).toBe(1));
    expect((sent[0].body as { credentials: unknown }).credentials).toEqual({
      aws_access_key_id: 'AKIAEXAMPLE',
      aws_secret_access_key: 'sekrit',
    });
  });

  test('a half-filled replacement is refused in the form, not sent', async () => {
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-replace')).toBeInTheDocument(),
    );
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
    // Nothing left the browser, and the message names the field and not
    // the value the user did type.
    expect(sent).toEqual([]);
    expect(
      screen.getByTestId('backend-form-problem').textContent,
    ).not.toContain('AKIAEXAMPLE');
  });

  test('keep and clear are unavailable when nothing is stored', async () => {
    // Offering them would let the form describe a state the row cannot
    // be in: there is nothing to keep and nothing to remove.
    domainsPayload = [
      domain({ backends: [backend({ credentials_set: false })] }),
    ];
    await mount(OPERATOR);
    await waitFor(() =>
      expect(screen.getByTestId('edit-backend-10')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('edit-backend-10'));
    await waitFor(() =>
      expect(screen.getByTestId('credential-mode-replace')).toBeChecked(),
    );
    expect(screen.getByTestId('credential-mode-keep')).toBeDisabled();
    expect(screen.getByTestId('credential-mode-clear')).toBeDisabled();
  });
});

describe('the zone rename (#75, ui-parity §3.3 G4)', () => {
  test('the form sends a PATCH, and nothing else', async () => {
    // `PATCH`, not `PUT`. The server leaves `PUT` at 405 deliberately,
    // so a client that sent one would get a method-not-allowed that
    // reads to an operator as "renaming does not work".
    await mount(OPERATOR);
    await waitFor(() =>
      expect(
        screen.getByTestId('rename-domain-example.net'),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('rename-domain-example.net'));

    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-name')).toBeInTheDocument(),
    );
    // The box opens on the current name, so a rename is an edit rather
    // than a re-type — and the submit is disabled until it changes.
    expect(screen.getByTestId('rename-zone-name')).toHaveValue('example.net');
    expect(screen.getByTestId('rename-zone-submit')).toBeDisabled();

    fireEvent.change(screen.getByTestId('rename-zone-name'), {
      target: { value: '  example.org  ' },
    });
    fireEvent.click(screen.getByTestId('rename-zone-submit'));

    await waitFor(() => expect(sent.length).toBe(1));
    expect(sent[0].method).toBe('PATCH');
    expect(sent[0].url).toContain('/atrium_ddns/domains/1');
    expect(sent[0].body).toEqual({ name: 'example.org' });
  });

  test('the form warns about the names before the server has to refuse', async () => {
    // The count is the server's own `hostname_count` for this zone, so
    // the warning cannot claim a number the row does not show.
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('rename-domain-example.net'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-warning')).toHaveTextContent(
        '3 names',
      ),
    );
    expect(screen.getByTestId('rename-zone-warning')).toHaveTextContent(
      'refused rather than rewriting them',
    );
  });

  test('a zone with no names says so instead of warning about nothing', async () => {
    domainsPayload = [domain({ hostname_count: 0 })];
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('rename-domain-example.net'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-warning')).toHaveTextContent(
        'no names in it',
      ),
    );
  });

  test("the server's refusal is shown verbatim, counts and all", async () => {
    // The 409 detail carries the number of names that would be orphaned
    // and a sample of them. Rewording it here would throw away the only
    // thing that makes the refusal actionable — and this bundle has no
    // second copy of `zone_contains` with which to predict it.
    const detail =
      "renaming 'example.net' to 'example.org' would leave 3 of 3 " +
      "hostnames outside the zone: 'box.example.net'";
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        if (url.endsWith('/users/me/context')) return json(OPERATOR);
        if (url.endsWith('/atrium_ddns/providers'))
          return json({ providers: PROVIDERS });
        if (url.endsWith('/atrium_ddns/domains') && method === 'GET')
          return json([domain()]);
        if (method === 'PATCH') return json({ detail }, 409);
        return json({});
      }),
    );
    await mount(OPERATOR);
    fireEvent.click(screen.getByTestId('rename-domain-example.net'));
    await waitFor(() =>
      expect(screen.getByTestId('rename-zone-name')).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId('rename-zone-name'), {
      target: { value: 'example.org' },
    });
    fireEvent.click(screen.getByTestId('rename-zone-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('domain-error')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('domain-error')).toHaveTextContent(
      '3 of 3 hostnames outside the zone',
    );
    expect(screen.getByTestId('domain-error')).toHaveTextContent(
      'box.example.net',
    );
  });
});
