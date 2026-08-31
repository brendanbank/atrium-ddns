import { randomBytes } from 'crypto';

import { expect } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { generate as generateTOTP } from 'otplib';

import { BOARD_PATH, DEVICES_PATH, DOMAINS_PATH } from '../src/paths';

/**
 * Shared vocabulary for the atrium-ddns e2e specs.
 *
 * Modelled on atrium's `frontend/tests-e2e/helpers.ts` — same function
 * names (`loginAsAdmin`, `loginAsUser`, `loginAndPassTOTP`), same
 * `API_URL` constant, same "drive auth over the API and hand the cookie
 * to the browser" shape — so someone who knows one harness can read the
 * other. Everything below `--- atrium-ddns specifics ---` is this
 * repo's own.
 */

// Fixture entropy. Not used to authenticate or sign anything — it keeps
// zone names, device names and email local-parts unique so consecutive
// runs against one long-lived stack do not collide on
// `uq_ddns_device_user_name` or on the global uniqueness of a zone.
function uniqueSuffix(): string {
  return `${Date.now().toString(36)}${randomBytes(2).readUInt16BE(0).toString(36)}`;
}

export const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:8053';
export const API_URL = process.env.E2E_API_URL ?? `${BASE_URL}/api`;

/** RFC 5737 TEST-NET-3. Every address this harness types, publishes or
 *  renders is documentation space, so a screenshot of the board can
 *  never carry a real one. The IPv6 twin is RFC 3849 `2001:db8::/32`. */
export const DOC_ADDRESS_V4 = '203.0.113.10';

/** RFC 6761 reserves `.invalid`; it can never be delegated, so a zone
 *  named under it cannot collide with anything real. Deliberately not
 *  used for **email** — atrium's validator refuses special-use domains
 *  in an address (`value is not a valid email address: The part after
 *  the @-sign is a special-use or reserved name`), so accounts use
 *  RFC 2606's `example.com`, which is atrium's own convention. */
export function uniqueZoneName(): string {
  return `z${uniqueSuffix()}.example.invalid`;
}

export function uniqueDeviceName(): string {
  return `router-${uniqueSuffix()}`;
}

function requiredAdminEnv(): {
  email: string;
  password: string;
  totpSecret: string;
} {
  const email = process.env.E2E_ADMIN_EMAIL;
  const password = process.env.E2E_ADMIN_PASSWORD;
  const totpSecret = process.env.E2E_ADMIN_TOTP_SECRET;
  if (!email || !password || !totpSecret) {
    throw new Error(
      'E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD / E2E_ADMIN_TOTP_SECRET must be ' +
        'set. Run the suite through `make test-e2e`, which seeds that admin ' +
        'and passes all three.',
    );
  }
  return { email, password, totpSecret };
}

/**
 * Log in and clear the TOTP challenge using the seeded secret.
 *
 * Drives `/auth/jwt/login` + `/auth/totp/verify` over the API rather
 * than typing into the `PinInput`, exactly as atrium's helper does: the
 * challenge widget has its own coverage upstream, and these specs are
 * about the host bundle's surfaces.
 */
export async function loginAndPassTOTP(
  page: Page,
  email: string,
  password: string,
  totpSecret: string,
): Promise<void> {
  const loginResp = await page.request.post(`${API_URL}/auth/jwt/login`, {
    form: { username: email, password },
  });
  if (!loginResp.ok() && loginResp.status() !== 204) {
    throw new Error(`login failed: ${loginResp.status()}`);
  }
  const code = await generateTOTP({ secret: totpSecret });
  const verifyResp = await page.request.post(`${API_URL}/auth/totp/verify`, {
    data: { code },
  });
  if (!verifyResp.ok() && verifyResp.status() !== 204) {
    throw new Error(
      `totp verify failed: ${verifyResp.status()} ${await verifyResp.text()}`,
    );
  }
  await syncCookies(page);
}

/** Surface the API context's cookie onto the browser jar. Playwright
 *  keeps the two jars separate on some versions; atrium's helper does
 *  the same dance and for the same reason. */
async function syncCookies(page: Page): Promise<void> {
  const cookies = await page.context().cookies();
  if (!cookies.some((cookie) => cookie.name === 'atrium_auth')) {
    const apiCookies = await page.request.storageState();
    await page.context().addCookies(apiCookies.cookies);
  }
}

/** Log in as the seeded super_admin. */
export async function loginAsSuperAdmin(page: Page): Promise<void> {
  const { email, password, totpSecret } = requiredAdminEnv();
  await loginAndPassTOTP(page, email, password, totpSecret);
}

/**
 * Alias of {@link loginAsSuperAdmin} for specs whose dependency is "any
 * admin who holds the permission" rather than super-admin specifically.
 * `make e2e-up` seeds one account holding both roles, so the two are
 * indistinguishable at the API level and the alias only keeps a spec's
 * intent readable.
 *
 * Both names exist because atrium's `helpers.ts` exports both, and the
 * point of copying its vocabulary is that a spec written against one
 * harness compiles against the other. That was not free advice: #90's
 * `hostname-suffix.spec.ts` was written against `loginAsSuperAdmin`,
 * this file shipped only `loginAsAdmin`, and `tsc` refused it the first
 * time the two met.
 */
export async function loginAsAdmin(page: Page): Promise<void> {
  await loginAsSuperAdmin(page);
}

export interface ProvisionedUser {
  email: string;
  password: string;
  totpSecret: string;
}

/**
 * Mint a fresh `user`-role tenant through atrium's own invite flow,
 * enrol it in TOTP, and leave `page` logged in as that tenant.
 *
 * The `user` role carries `atrium_ddns.domain.manage`,
 * `atrium_ddns.device.manage` and `atrium_ddns.hostname.manage`
 * (`0002_ddns_core`) and **not** `atrium_ddns.write` or
 * `app_setting.manage` — which is what makes it the right account both
 * for the §3.3.1 walk and for the negative spec.
 */
export async function loginAsUser(page: Page): Promise<ProvisionedUser> {
  const admin = requiredAdminEnv();
  const context = page.context();
  const request = context.request;

  const adminLogin = await request.post(`${API_URL}/auth/jwt/login`, {
    form: { username: admin.email, password: admin.password },
  });
  if (!adminLogin.ok() && adminLogin.status() !== 204) {
    throw new Error(`admin login failed: ${adminLogin.status()}`);
  }
  const adminCode = await generateTOTP({ secret: admin.totpSecret });
  const adminVerify = await request.post(`${API_URL}/auth/totp/verify`, {
    data: { code: adminCode },
  });
  if (!adminVerify.ok() && adminVerify.status() !== 204) {
    throw new Error(`admin totp verify failed: ${adminVerify.status()}`);
  }

  const email = `e2e-${uniqueSuffix()}@example.com`;
  const password = 'Tenant-Pw-12345!';
  const inviteResp = await request.post(`${API_URL}/invites`, {
    data: { email, full_name: 'E2E Tenant', role_codes: ['user'] },
  });
  if (inviteResp.status() !== 201) {
    throw new Error(
      `invite create failed: ${inviteResp.status()} ${await inviteResp.text()}`,
    );
  }
  const invite = (await inviteResp.json()) as { token: string };

  // Drop the admin cookie before anything user-side, so the invite is
  // not accepted under the admin's session.
  await context.clearCookies();

  const acceptResp = await request.post(`${API_URL}/invites/accept`, {
    data: { token: invite.token, password },
  });
  if (!acceptResp.ok() && acceptResp.status() !== 201) {
    throw new Error(
      `invite accept failed: ${acceptResp.status()} ${await acceptResp.text()}`,
    );
  }
  const userLogin = await request.post(`${API_URL}/auth/jwt/login`, {
    form: { username: email, password },
  });
  if (!userLogin.ok() && userLogin.status() !== 204) {
    throw new Error(`user login failed: ${userLogin.status()}`);
  }

  // Enrol TOTP and confirm, which flips `totp_passed=True` on the
  // session row so the host's endpoints accept the cookie.
  const setupResp = await request.post(`${API_URL}/auth/totp/setup`);
  if (!setupResp.ok()) {
    throw new Error(
      `totp setup failed: ${setupResp.status()} ${await setupResp.text()}`,
    );
  }
  const { secret } = (await setupResp.json()) as { secret: string };
  const code = await generateTOTP({ secret });
  const confirmResp = await request.post(`${API_URL}/auth/totp/confirm`, {
    data: { code },
  });
  if (!confirmResp.ok() && confirmResp.status() !== 204) {
    throw new Error(
      `totp confirm failed: ${confirmResp.status()} ${await confirmResp.text()}`,
    );
  }

  await syncCookies(page);
  return { email, password, totpSecret: secret };
}

// --- atrium-ddns specifics ---------------------------------------- //

/** The paths the bundle registers, imported from the module the
 *  registrations themselves read (`src/paths.ts`) rather than typed a
 *  second time here. `NAMES_PATH` and `LOG_PATH` live in their own
 *  page modules — `.tsx`, so importing them would pull Mantine and
 *  React into the test process — and are the two literals below. */
export const NAMES_PATH = '/atrium-ddns/names';
export const LOG_PATH = '/atrium-ddns/logs';
export { BOARD_PATH, DEVICES_PATH, DOMAINS_PATH };

/** Every nav item the bundle registers, label and destination.
 *  `ui-parity.md` §3.2 reads the same seven out of the served bundle. */
/** Every nav item the bundle registers, and where each goes.
 *
 * Four, not seven. The board became the landing surface, so its own item
 * and the root's collapsed into one; `Devices` and `Names` lost their
 * entries because a device is reached from the board and a name from its
 * zone. **The routes still exist** — this list is the sidebar, not the
 * set of addresses that resolve, and a spec that conflated the two would
 * fail the day a page became reachable only by link. */
export const DDNS_NAV_ITEMS: ReadonlyArray<{ label: string; to: string }> = [
  { label: 'Devices and names', to: '/atrium-ddns' },
  { label: 'Zones and providers', to: DOMAINS_PATH },
  { label: 'Log search', to: LOG_PATH },
  { label: 'Help', to: '/atrium-ddns/help' },
];

/**
 * Bind one of the compat fixture's scripted provider slots to a zone.
 *
 * **This is the one step of the §3.3.1 walk that is not driven through
 * the UI, and the reason is a deliberate product decision rather than a
 * gap in the harness.** A strip only renders once a name has been
 * *published* (`router.py::_strips_for` — a family appears when
 * `last_ip_*` or `dns_ip_*` is set), and `persist_updates` writes
 * `last_ip_*` on `good` only. The only provider that can answer `good`
 * without contacting a real nameserver is `compat_stub`'s scripted
 * slot, and the catalogue behind the UI's provider `Select`
 * (`GET /providers` -> `known_services()`) deliberately does not offer
 * it: `backend/tests/test_compat_stub.py` asserts
 * `set(SLOTS).isdisjoint(known_services())` under the heading
 * *"`known_services()` is what a UI offers when creating a backend"*.
 *
 * So this call is `ui-parity.md` §3.3.1 step 4 verbatim — the same
 * `POST /domains/{id}/backends` the parity walk makes — and every other
 * step of the walk is a click. Widening the catalogue to make the click
 * possible would delete a guard the product wrote on purpose.
 *
 * `credentials` matters: a slot with *no* stored credential is exactly
 * how the frozen table manufactures `911`, so an empty object here
 * produces a walk that ends in `911 <ip>` and no strip.
 */
export async function bindScriptedBackend(
  request: APIRequestContext,
  domainId: number,
  options: { result?: string; ttl?: number } = {},
): Promise<{ id: number; backend_type: string; credentials_set: boolean }> {
  const resp = await request.post(
    `${API_URL}/atrium_ddns/domains/${domainId}/backends`,
    {
      data: {
        backend_type: 'stub1',
        config: { result: options.result ?? 'good', ttl: options.ttl ?? 300 },
        credentials: { stub_token: 'e2e-fixture-not-a-secret' },
      },
    },
  );
  if (resp.status() !== 201) {
    throw new Error(
      `binding the scripted backend failed: ${resp.status()} ` +
        `${await resp.text()}\n` +
        'The e2e stack must run with ATRIUM_DDNS_COMPAT_STUB=1 (and not ' +
        'ENVIRONMENT=prod) or `stub1` resolves to no adapter. ' +
        '`make e2e-up` sets it.',
    );
  }
  return (await resp.json()) as {
    id: number;
    backend_type: string;
    credentials_set: boolean;
  };
}

/**
 * The device calling in, over HTTP Basic, exactly as a router does —
 * §3.3.1 step 8. Returns the wire body (`good 203.0.113.10`,
 * `nochg …`, `911 …`, `badauth`, …).
 *
 * `X-Forwarded-For` is set on purpose and is not decoration.
 * `auth_device.client_address` takes the rightmost forwarded element,
 * so this pins the address recorded as *called from* — the strip's
 * third station — to RFC 5737 documentation space. Without it the row
 * carries whatever socket address the container saw, which is both
 * machine-dependent (so the screenshot is not reproducible) and the one
 * value on that surface that could be a real address.
 */
export async function deviceCallsIn(
  request: APIRequestContext,
  options: {
    username: string;
    secret: string;
    hostname: string;
    ip?: string;
    clientIp?: string;
  },
): Promise<{ status: number; body: string }> {
  const ip = options.ip ?? DOC_ADDRESS_V4;
  const credentials = Buffer.from(
    `${options.username}:${options.secret}`,
  ).toString('base64');
  const resp = await request.get(
    `${BASE_URL}/nic/update?hostname=${encodeURIComponent(options.hostname)}` +
      `&myip=${encodeURIComponent(ip)}`,
    {
      headers: {
        Authorization: `Basic ${credentials}`,
        'X-Forwarded-For': options.clientIp ?? DOC_ADDRESS_V4,
      },
    },
  );
  return { status: resp.status(), body: (await resp.text()).trim() };
}

/**
 * Pick a value from a Mantine `Select`.
 *
 * A Mantine v9 `Select` is an input plus a portalled dropdown, not a
 * native `<select>`: `selectOption()` does not apply, and the option
 * text is the `label`, never the `value`. Anchoring on the testid the
 * component was given keeps this off `getByLabel`, which matches the
 * asterisk on required fields and matches two elements when a modal
 * repeats a label the page already has.
 */
export async function chooseFromSelect(
  page: Page,
  testId: string,
  optionLabel: string,
): Promise<void> {
  const input = page.getByTestId(testId);
  const option = page.getByRole('option', { name: optionLabel, exact: true });

  // Open, choose, and **verify** — then retry once if the value did not
  // land.
  //
  // Mantine's `Select` renders its options into a portal after the click,
  // and the catalogue behind this one arrives from `GET /providers`. When
  // that response is warm the list can paint a beat after the dropdown
  // opens, so the click lands on a list that is still empty and the
  // helper returns having done nothing. The caller then clicks a submit
  // that is disabled, and waits for it — which is why this surfaced as a
  // thirty-second timeout naming a button rather than a select.
  //
  // Asserting the value here is what makes the failure legible; the retry
  // is what makes it rare. Both, not either: a retry alone would hide a
  // genuinely missing option behind a second attempt that also fails.
  for (const attempt of [1, 2]) {
    await input.click();
    await option.waitFor({ state: 'visible', timeout: 5_000 });
    await option.click();
    try {
      await expect(input).toHaveValue(optionLabel, { timeout: 2_000 });
      return;
    } catch (error) {
      if (attempt === 2) throw error;
    }
  }
}

/** Seed a zone, a scripted provider, a device and a published name.
 *
 * Written because three specs were each doing this by hand, in slightly
 * different orders, and the board rewrite broke all three at once. The
 * publish goes through `deviceCallsIn` — the wire, as a router drives it
 * — because a name only gains a strip once a `good` aggregate has landed
 * (`persist_updates` writes `last_ip_*` on `good` only).
 */
export async function seedZoneDeviceAndName(
  page: Page,
  opts: { zone: string; deviceName: string },
): Promise<SeededName> {
  const api = page.request;
  const zoneRes = await api.post(`${API_URL}/atrium_ddns/domains`, {
    data: { name: opts.zone },
  });
  const domainId = (await zoneRes.json()).id as number;
  await bindScriptedBackend(api, domainId);

  const devRes = await api.post(`${API_URL}/atrium_ddns/devices`, {
    data: { name: opts.deviceName, rate_limit_per_minute: null },
  });
  const issued = await devRes.json();
  const deviceId = (issued.device ?? issued).id as number;
  const username = (issued.device ?? issued).username as string;
  const secret = (issued.secret ?? issued.password) as string;

  const hostname = `home.${opts.zone}`;
  await api.post(`${API_URL}/atrium_ddns/hostnames`, {
    data: { name: hostname, domain_id: domainId, device_id: deviceId },
  });

  await deviceCallsIn(api, { username, secret, hostname, ip: DOC_ADDRESS_V4 });
  return {
    hostname,
    domainId,
    deviceId,
    deviceName: opts.deviceName,
    username,
    secret,
    family: 'A',
  };
}

/** What {@link seedZoneDeviceAndName} leaves behind.
 *
 * The device's own credentials are part of it because the log is the one
 * surface where *a second call from the same device* is the fixture:
 * every line on it is written by the wire, so a spec that needs two
 * lines needs to be able to call in twice. Returning them beats a
 * second seeding path that re-issues a device to get at them.
 */
export interface SeededName {
  hostname: string;
  domainId: number;
  deviceId: number;
  deviceName: string;
  /** The DDNS username the device authenticates with, not the account's. */
  username: string;
  /** Shown once by `POST /devices` and never again. Fixture-only. */
  secret: string;
  family: 'A';
}
