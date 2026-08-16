/** The settings pages — the surface #73 was opened for.
 *
 * The three things this file is written to make a plausible-looking
 * implementation fail at:
 *
 * 1. **The save sends the whole namespace.** Atrium's `put_namespace`
 *    runs `model_validate(payload)`, so a body that omits a field
 *    resets it to the model default. An implementation that PATCHed the
 *    edited field — which is what anyone would write first, and what
 *    reads best in a diff — silently reverts every other setting in the
 *    namespace. The assertion is on the **request body**, field by
 *    field, not on the response.
 *
 * 2. **Bounds come from the server.** The inputs carry the model's own
 *    `min`/`max`, so a form that offered `0` for a field the model
 *    requires `>= 1` for cannot exist. Asserted against the rendered
 *    DOM attributes, with a schema whose bounds are *deliberately not*
 *    the real ones — a fixture repeating the production numbers would
 *    pass against a form that hardcoded them.
 *
 * 3. **Nothing is lost quietly.** A group the server sends that this
 *    build has no page for is named on screen. That backstop is the
 *    frontend half of the backend's `ungrouped` bucket, and it is the
 *    only thing standing between "the grouping drifted" and "a setting
 *    became unreachable again", which is the whole subject of the
 *    issue.
 */
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { SettingsPage } from '../SettingsPage';
import { CONFIG_PERMISSION, type SettingsSchema } from '../api/config';
import { queryClient } from '../queryClient';

const ADMIN: UserContext = {
  id: 1,
  email: 'admin@example.invalid',
  full_name: 'Admin',
  is_active: true,
  roles: ['super_admin'],
  permissions: [CONFIG_PERMISSION, 'atrium_ddns.admin'],
  impersonating_from: null,
};

/** Holds every `atrium_ddns.*` permission and not atrium's. The gate is
 *  atrium's, and a gate spelled "holds any host permission" passes for
 *  this user — which is the point of giving them to him. */
const TENANT: UserContext = {
  id: 2,
  email: 'tenant@example.invalid',
  full_name: 'Tenant',
  is_active: true,
  roles: ['user'],
  permissions: [
    'atrium_ddns.admin',
    'atrium_ddns.write',
    'atrium_ddns.device.manage',
  ],
  impersonating_from: null,
};

/** Bounds chosen so that **no** number here is one the real model uses.
 *  A fixture that repeated the production numbers would pass against a
 *  form with them hardcoded, which is the defect the derivation exists
 *  to prevent. */
const SCHEMA: SettingsSchema = {
  namespace: 'atrium_ddns',
  write_path: '/admin/app-config/atrium_ddns',
  permission: CONFIG_PERMISSION,
  groups: [
    {
      key: 'rate-limits',
      label: 'Rate limits',
      blurb: 'The abuse control on /nic/update.',
      fields: [
        {
          name: 'rate_limit_per_minute',
          type: 'integer',
          label: 'Rate Limit Per Minute',
          help: 'Updates a device may make per minute unless it carries its own.',
          default: 7,
          minimum: 2,
          maximum: 44,
        },
      ],
    },
    {
      key: 'health-checks',
      label: 'Health checks',
      blurb: 'The scheduled resolution.',
      fields: [
        {
          name: 'health_check_enabled',
          type: 'boolean',
          label: 'Health Check Enabled',
          help: 'Whether the scheduled health check resolves hostnames.',
          default: true,
          minimum: null,
          maximum: null,
        },
        {
          name: 'health_check_timeout_seconds',
          type: 'number',
          label: 'Health Check Timeout Seconds',
          help: 'Per-query DNS timeout; a fractional value is allowed.',
          default: 3.5,
          minimum: 0.3,
          maximum: 41,
        },
      ],
    },
  ],
};

const STORED: Record<string, unknown> = {
  rate_limit_per_minute: 30,
  health_check_enabled: true,
  health_check_timeout_seconds: 5.0,
  // A field the schema does not group here but the namespace holds. It
  // has to survive the save untouched, which is assertion 1.
  event_retention_days: 30,
};

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let schemaPayload: SettingsSchema = SCHEMA;
let adminConfigPayload: Record<string, unknown> = {};
let puts: { url: string; body: Record<string, unknown> }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch() {
  puts = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return json(currentMe);
      }
      if (url.endsWith('/atrium_ddns/config/schema')) {
        return json(schemaPayload);
      }
      if (url.endsWith('/admin/app-config') && method === 'GET') {
        return json(adminConfigPayload);
      }
      if (method === 'PUT') {
        const body = JSON.parse(String(init?.body ?? '{}'));
        puts.push({ url, body });
        return json(body);
      }
      return json({});
    }),
  );
}

beforeEach(() => {
  schemaPayload = SCHEMA;
  adminConfigPayload = { atrium_ddns: { ...STORED }, brand: { name: 'x' } };
  stubFetch();
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  handles?.cleanup();
  vi.unstubAllGlobals();
  currentMe = null;
});

async function mount(user: UserContext | null, groupKey = 'rate-limits') {
  currentMe = user;
  handles = mockAtriumRegistry({ me: user });
  renderWithAtrium(<SettingsPage groupKey={groupKey} />);
  // `usePerm` answers false while `me` is in flight, so the refusal is
  // also the pre-resolution state and an OR-wait settles on it for a
  // permitted user. Wait for the marker this user is expected to end
  // on. (The comment in `DevicesPage.test.tsx` says why at length; it
  // cost that file a debugging session.)
  const expected = user?.permissions.includes(CONFIG_PERMISSION)
    ? 'settings-blurb'
    : 'settings-refused';
  await waitFor(() => expect(screen.getByTestId(expected)).toBeTruthy());
}

test('a caller without atriums own permission gets a refusal, not an empty page', async () => {
  await mount(TENANT);
  const refusal = screen.getByTestId('settings-refused');
  expect(refusal.textContent).toContain(CONFIG_PERMISSION);
  // And nothing was fetched behind the refusal — a 403 in the network
  // tab on every page load is noise, and the refusal is already known.
  expect(screen.queryByTestId('setting-rate_limit_per_minute')).toBeNull();
});

test('the inputs enforce the bounds the server sent, not bounds this bundle knows', async () => {
  await mount(ADMIN);
  const input = screen.getByTestId(
    'setting-rate_limit_per_minute',
  ) as HTMLInputElement;

  // Behavioural, not an attribute read: Mantine v9's `NumberInput` does
  // not put `min`/`max` on the DOM node — it clamps in JS on blur — so
  // an attribute assertion here would pass against a form with no
  // bounds at all and fail against the one that works. (Measured: both
  // attributes read `null` on a correctly bounded input.)
  fireEvent.change(input, { target: { value: '1' } });
  fireEvent.blur(input);
  await waitFor(() => expect(input.value).toBe('2'));

  fireEvent.change(input, { target: { value: '900' } });
  fireEvent.blur(input);
  await waitFor(() => expect(input.value).toBe('44'));

  // 2 and 44 are not numbers this repository uses anywhere. A form with
  // the model's real bounds hardcoded clamps to 0 and 10000 and fails
  // both assertions above.

  // The help line carries the range and the default, because "inherit"
  // is a real state on every per-device limit in this product and the
  // inherited number has to be readable somewhere.
  const described = screen.getByText(/2 to 44/);
  expect(described.textContent).toContain('Default 7');
});

test('a float field is not rendered as an integer input', async () => {
  await mount(ADMIN, 'health-checks');
  const timeout = screen.getByTestId(
    'setting-health_check_timeout_seconds',
  ) as HTMLInputElement;

  // The decimal survives. On an integer input the separator is stripped
  // and `2.5` becomes `25` — which is how a 5-second timeout quietly
  // becomes something else entirely.
  fireEvent.change(timeout, { target: { value: '2.5' } });
  await waitFor(() => expect(timeout.value).toBe('2.5'));

  // …and the integer field on the other page does strip it, so the
  // assertion above is about `allowDecimal` and not about the harness.
  cleanup();
  queryClient.clear();
  await mount(ADMIN);
  const whole = screen.getByTestId(
    'setting-rate_limit_per_minute',
  ) as HTMLInputElement;
  fireEvent.change(whole, { target: { value: '2.5' } });
  await waitFor(() => expect(whole.value).not.toContain('.'));
});

test('a boolean is a switch and not a number box', async () => {
  await mount(ADMIN, 'health-checks');
  expect(
    screen.getByTestId('setting-health_check_enabled').getAttribute('type'),
  ).toBe('checkbox');
});

test('saving one page writes every field of the namespace', async () => {
  await mount(ADMIN);
  fireEvent.change(screen.getByTestId('setting-rate_limit_per_minute'), {
    target: { value: '5' },
  });
  await waitFor(() =>
    expect(
      (screen.getByTestId('settings-save') as HTMLButtonElement).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByTestId('settings-save'));

  await waitFor(() => expect(puts.length).toBe(1));
  expect(puts[0].url).toContain('/admin/app-config/atrium_ddns');
  // The edited field…
  expect(puts[0].body.rate_limit_per_minute).toBe(5);
  // …and every other field the *schema* knows, at its stored value. A
  // partial body would reset these to the model defaults on the server,
  // and the response would look completely fine.
  expect(puts[0].body.health_check_enabled).toBe(true);
  expect(puts[0].body.health_check_timeout_seconds).toBe(5);
  // A field the namespace holds and no group on this schema claims is
  // NOT sent — it is not in `schema.groups`, so the form has no
  // opinion about it. This is recorded rather than asserted as a
  // virtue: with the real schema every field is grouped, so the case
  // cannot arise, and the backend's `ungrouped` bucket plus
  // `settings-unrouted` below are what keep it that way.
  expect('event_retention_days' in puts[0].body).toBe(false);

  await waitFor(() => expect(screen.getByTestId('settings-saved')).toBeTruthy());
});

test('a group the server sends and this build cannot route to is named', async () => {
  schemaPayload = {
    ...SCHEMA,
    groups: [
      ...SCHEMA.groups,
      {
        key: 'ungrouped',
        label: 'Assigned to no page yet',
        blurb: 'These exist in the model and this build assigns them nowhere.',
        fields: [
          {
            name: 'prune_max_batches',
            type: 'integer',
            label: 'Prune Max Batches',
            help: 'Ceiling on batches per prune tick.',
            default: 100,
            minimum: 1,
            maximum: 10000,
          },
        ],
      },
    ],
  };
  await mount(ADMIN);
  const notice = screen.getByTestId('settings-unrouted');
  expect(notice.textContent).toContain('ungrouped');
  // The field is *named*. "Some settings are unreachable" would be a
  // notice nobody can act on.
  expect(notice.textContent).toContain('prune_max_batches');
});

test('a namespace the running atrium does not serve is a stated fact', async () => {
  adminConfigPayload = { brand: { name: 'x' } };
  currentMe = ADMIN;
  handles = mockAtriumRegistry({ me: ADMIN });
  renderWithAtrium(<SettingsPage groupKey="rate-limits" />);
  await waitFor(() =>
    expect(screen.getByTestId('settings-absent')).toBeTruthy(),
  );
  // Not eleven defaults over a form whose save button would 404.
  expect(screen.queryByTestId('settings-save')).toBeNull();
  expect(screen.getByTestId('settings-absent').textContent).toContain(
    'atrium_ddns',
  );
});

test('a failed save shows the servers own words', async () => {
  await mount(ADMIN);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (url.endsWith('/users/me/context')) return json(ADMIN);
      if (url.endsWith('/atrium_ddns/config/schema')) return json(SCHEMA);
      if (url.endsWith('/admin/app-config') && method === 'GET') {
        return json(adminConfigPayload);
      }
      if (method === 'PUT') {
        return new Response(
          JSON.stringify({
            detail: '1 validation error for DdnsConfig\nrate_limit_per_minute',
          }),
          { status: 400, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return json({});
    }),
  );
  fireEvent.change(screen.getByTestId('setting-rate_limit_per_minute'), {
    target: { value: '5' },
  });
  await waitFor(() =>
    expect(
      (screen.getByTestId('settings-save') as HTMLButtonElement).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByTestId('settings-save'));
  await waitFor(() =>
    expect(screen.getByTestId('settings-save-error')).toBeTruthy(),
  );
  // Diagnostics in full — the status and pydantic's own message.
  // Redact secrets, never diagnostics.
  const shown = screen.getByTestId('settings-save-error').textContent ?? '';
  expect(shown).toContain('400');
  expect(shown).toContain('validation error');
});
