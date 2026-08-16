/** Publishing — the modal #74 added, and the one thing it must not say.
 *
 * **An empty selection means *inherit the zone*, not *publish
 * nowhere*.** That is the migration's safety property: every hostname
 * that existed before `0004` has no selection rows, so the wrong
 * rendering tells the owner of a perfectly working name that it
 * publishes to nothing — on every screen, for every name nobody has
 * ever edited. A set of unticked checkboxes reads exactly that way, so
 * the assertion below is not on the checkboxes but on the sentence
 * above them.
 *
 * The rest: what actually left the browser on save, that the three-level
 * TTL is rendered as three levels, that a manual update reports each
 * backend rather than only the aggregate, and that a 429 is the server's
 * own sentence rather than a generic failure.
 *
 * No hostname or address validation is asserted here because there is
 * none to assert — `api/hostnames.ts` says why. The one client-side
 * check in the modal is the TTL range, and its bounds are the *server's
 * own numbers*, shipped in the payload as `ttl_min` / `ttl_max`: one
 * source of truth, rendered early. The server still refuses.
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
import {
  HOSTNAME_PERMISSION,
  type Hostname,
  type HostnamePublishing,
  type ManualUpdateResult,
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

const ZONE = 'example.invalid';
const NAME = `home.${ZONE}`;

function domain(): Domain {
  return {
    id: 1,
    name: ZONE,
    created_at: '2026-08-15T10:00:00Z',
    backends: [],
    hostname_count: 1,
  };
}

function device(): Device {
  return {
    id: 7,
    name: 'attic-router',
    username: 'ddns-abc123',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-16T09:00:00Z',
    rate_limit_per_minute: null,
    // #73's field: the device's own limit resolved against the
    // namespace default. `null` above means *inherit*, and this is what
    // it inherits — the same number the manual update reports back as
    // `rate_limit_per_minute`, because both spend the same budget.
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued',
    hostname_count: 1,
  };
}

function hostname(overrides: Partial<Hostname> = {}): Hostname {
  return {
    id: 100,
    name: NAME,
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

/** The state every pre-`0004` row is in: three bindings on the zone,
 *  none selected, no TTL override — and therefore publishing to all
 *  three at the service default. */
function inheriting(
  overrides: Partial<HostnamePublishing> = {},
): HostnamePublishing {
  return {
    hostname_id: 100,
    name: NAME,
    domain_id: 1,
    domain_name: ZONE,
    device_id: 7,
    inherits_backends: true,
    ttl: null,
    default_ttl: 60,
    ttl_min: 30,
    ttl_max: 86400,
    backends: [
      {
        backend_id: 11,
        backend_type: 'route53',
        selected: false,
        credentials_set: true,
        binding_ttl: null,
        effective_ttl: 60,
      },
      {
        backend_id: 12,
        backend_type: 'hetzner',
        selected: false,
        credentials_set: true,
        binding_ttl: null,
        effective_ttl: 60,
      },
      {
        backend_id: 13,
        backend_type: 'nsupdate',
        selected: false,
        credentials_set: false,
        binding_ttl: null,
        effective_ttl: 60,
      },
    ],
    publishes_to: [11, 12, 13],
    ...overrides,
  };
}

let handles: MockAtriumHandles;
let publishingPayload: HostnamePublishing = inheriting();
let updateResult: ManualUpdateResult | null = null;
let sent: { url: string; method: string; body: any }[] = [];
let nextFailure: { status: number; detail: string } | null = null;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
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
      if (url.endsWith('/users/me/context')) return json(OPERATOR);
      if (url.endsWith('/atrium_ddns/hostnames') && method === 'GET') {
        return json([hostname()]);
      }
      if (url.endsWith('/atrium_ddns/domains') && method === 'GET') {
        return json([domain()]);
      }
      if (url.endsWith('/atrium_ddns/devices') && method === 'GET') {
        return json([device()]);
      }
      if (url.endsWith('/backends')) return json(publishingPayload);
      if (url.endsWith('/update')) return json(updateResult);
      return json({});
    }),
  );
}

beforeEach(() => {
  stubFetch();
  publishingPayload = inheriting();
  updateResult = null;
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  handles?.cleanup();
  vi.unstubAllGlobals();
});

async function openPublishing() {
  handles = mockAtriumRegistry({ me: OPERATOR });
  renderWithAtrium(<HostnamesPage />);
  await waitFor(() =>
    expect(screen.getByTestId('add-hostname')).toBeInTheDocument(),
  );
  fireEvent.click(screen.getByTestId(`publishing-${NAME}`));
  await waitFor(() =>
    expect(screen.getByTestId('publishing-summary')).toBeInTheDocument(),
  );
}

describe('an empty selection is inheritance, not silence', () => {
  test('the summary says inheriting, and names the count it resolved to', async () => {
    await openPublishing();

    const summary = screen.getByTestId('publishing-summary').textContent ?? '';
    expect(summary).toContain('Inheriting the zone');
    expect(summary).toContain('all 3');
    expect(summary).not.toMatch(/publishes to nothing/i);
  });

  test('no checkbox is ticked, and the note says what that means', async () => {
    await openPublishing();

    // The stored state — every box unticked — is rendered honestly.
    for (const type of ['route53', 'hetzner', 'nsupdate']) {
      expect(screen.getByTestId(`publish-to-${type}`)).not.toBeChecked();
    }
    // …and the effect is stated, because unticked boxes alone read as
    // the opposite of what they mean.
    expect(
      screen.getByTestId('publishing-empty-note').textContent,
    ).toContain('not "publish nowhere"');
  });

  test('a zone with no bindings says 911, which is a different sentence', async () => {
    publishingPayload = inheriting({ backends: [], publishes_to: [] });
    await openPublishing();

    expect(screen.getByTestId('publishing-summary').textContent).toContain(
      'publishes to nothing',
    );
    expect(screen.getByTestId('publishing-no-backends')).toBeInTheDocument();
  });

  test('an explicit selection reports the subset, not inheritance', async () => {
    const backends = inheriting().backends.map((b) => ({
      ...b,
      selected: b.backend_id === 12,
    }));
    publishingPayload = inheriting({
      backends,
      inherits_backends: false,
      publishes_to: [12],
    });
    await openPublishing();

    expect(screen.getByTestId('publishing-summary').textContent).toContain(
      'Publishes to 1 of 3',
    );
    expect(screen.getByTestId('publish-to-hetzner')).toBeChecked();
    expect(screen.getByTestId('publish-to-route53')).not.toBeChecked();
  });
});

describe('what leaves the browser', () => {
  test('save sends the ticked ids and a null TTL for an empty field', async () => {
    await openPublishing();

    fireEvent.click(screen.getByTestId('publish-to-hetzner'));
    fireEvent.click(screen.getByTestId('publishing-save'));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].method).toBe('PUT');
    expect(sent[0].url).toContain('/atrium_ddns/hostnames/100/backends');
    expect(sent[0].body).toEqual({ backend_ids: [12], ttl: null });
  });

  test('an empty TTL field is null, never the default rendered as a value', async () => {
    // The failure this catches: seeding the input with `default_ttl`
    // and posting it back turns "inherits, follows the zone" into
    // "explicitly 60, follows nothing" for every name anyone opens.
    await openPublishing();
    expect(screen.getByTestId('publishing-ttl')).toHaveValue('');

    fireEvent.click(screen.getByTestId('publishing-save'));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body.ttl).toBeNull();
  });

  test('a TTL outside the server-supplied bounds blocks the save', async () => {
    await openPublishing();

    fireEvent.change(screen.getByTestId('publishing-ttl'), {
      target: { value: '5' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('publishing-save')).toBeDisabled(),
    );
    expect(sent).toHaveLength(0);

    fireEvent.change(screen.getByTestId('publishing-ttl'), {
      target: { value: '30' },
    });
    await waitFor(() =>
      expect(screen.getByTestId('publishing-save')).not.toBeDisabled(),
    );
  });

  test('a saved TTL is sent as a number', async () => {
    await openPublishing();
    fireEvent.change(screen.getByTestId('publishing-ttl'), {
      target: { value: '300' },
    });
    fireEvent.click(screen.getByTestId('publishing-save'));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0].body.ttl).toBe(300);
  });
});

describe('publish now', () => {
  test('every backend is reported, not only the aggregate', async () => {
    updateResult = {
      hostname_id: 100,
      name: NAME,
      ip: '203.0.113.10',
      rtype: 'A',
      status: 'good',
      published: true,
      rate_limit_per_minute: 30,
      success_response_codes: ['good', 'nochg'],
      attempts: [
        { backend_id: 11, backend_type: 'route53', status: 'good' },
        { backend_id: 12, backend_type: 'hetzner', status: 'dnserr' },
      ],
    };
    await openPublishing();

    fireEvent.change(screen.getByTestId('publishing-ip'), {
      target: { value: '203.0.113.10' },
    });
    fireEvent.click(screen.getByTestId('publishing-update'));

    await waitFor(() =>
      expect(screen.getByTestId('publishing-result')).toBeInTheDocument(),
    );
    // The aggregate is `good`; one backend answered `dnserr`. An
    // aggregate-only rendering hides exactly this.
    const failed = screen.getByTestId('publishing-attempt-hetzner');
    const settled = screen.getByTestId('publishing-attempt-route53');
    expect(failed.textContent).toContain('dnserr');
    expect(settled.textContent).toContain('good');
    // §1.2 Rule 1: agreement has no colour, and Rule 3: the accent is
    // never the only channel. The tone comes from the server's own
    // success list, shipped in the payload.
    expect(failed.getAttribute('data-tone')).toBe('diverge');
    expect(settled.getAttribute('data-tone')).toBe('ink');
    expect(failed.textContent).toContain('≠');
    expect(settled.textContent).not.toContain('≠');
    expect(sent[0].method).toBe('POST');
    expect(sent[0].body).toEqual({ ip: '203.0.113.10' });
  });

  test('a 429 renders the server sentence about the shared budget', async () => {
    await openPublishing();
    nextFailure = {
      status: 429,
      detail:
        'attic-router is over its rate limit of 30 per minute. A manual '
        + 'update draws on the same budget as the device’s own calls.',
    };
    fireEvent.change(screen.getByTestId('publishing-ip'), {
      target: { value: '203.0.113.10' },
    });
    fireEvent.click(screen.getByTestId('publishing-update'));

    await waitFor(() =>
      expect(screen.getByTestId('publishing-error')).toBeInTheDocument(),
    );
    const rendered = screen.getByTestId('publishing-error').textContent ?? '';
    expect(rendered).toContain('429');
    expect(rendered).toContain('same budget');
    expect(screen.queryByTestId('publishing-result')).toBeNull();
  });

  test('an unassigned name cannot publish, and is told why', async () => {
    publishingPayload = inheriting({ device_id: null });
    handles = mockAtriumRegistry({ me: OPERATOR });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        if (method !== 'GET') {
          sent.push({ url, method, body: undefined });
        }
        if (url.endsWith('/users/me/context')) return json(OPERATOR);
        if (url.endsWith('/atrium_ddns/hostnames') && method === 'GET') {
          return json([hostname({ device_id: null, device_name: null })]);
        }
        if (url.endsWith('/atrium_ddns/domains')) return json([domain()]);
        if (url.endsWith('/atrium_ddns/devices')) return json([device()]);
        if (url.endsWith('/backends')) return json(publishingPayload);
        return json({});
      }),
    );
    renderWithAtrium(<HostnamesPage />);
    await waitFor(() =>
      expect(screen.getByTestId('add-hostname')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId(`publishing-${NAME}`));
    await waitFor(() =>
      expect(screen.getByTestId('publishing-no-device')).toBeInTheDocument(),
    );

    expect(screen.getByTestId('publishing-update')).toBeDisabled();
    expect(screen.getByTestId('publishing-ip')).toBeDisabled();
    expect(
      screen.getByTestId('publishing-no-device').textContent,
    ).toContain('rate-limit budget');
  });
});

describe('the three-level TTL is rendered as three levels', () => {
  test('the per-binding effective TTL is shown beside each backend', async () => {
    publishingPayload = inheriting({
      ttl: null,
      backends: inheriting().backends.map((b, index) => ({
        ...b,
        binding_ttl: index === 0 ? 300 : null,
        effective_ttl: index === 0 ? 300 : 60,
      })),
    });
    await openPublishing();

    // Two bindings on one zone legitimately answering differently is
    // the state a single "TTL: 60" line would erase.
    expect(screen.getByTestId('publish-label-route53').textContent).toContain(
      '300s',
    );
    expect(screen.getByTestId('publish-label-hetzner').textContent).toContain(
      '60s',
    );
  });

  test('a binding with no credentials says so where it is chosen', async () => {
    await openPublishing();
    expect(screen.getByTestId('publish-label-nsupdate').textContent).toContain(
      '911',
    );
  });
});
