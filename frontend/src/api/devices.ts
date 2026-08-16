/** `/api/atrium_ddns/devices` — create, rotate, delete.
 *
 * ## The secret exists twice, and only twice
 *
 * `DeviceSecret` is the only type in this bundle that carries a device
 * secret, and it is the return type of exactly two calls: `createDevice`
 * and `rotateDeviceSecret`. `Device` — what every *read* returns — has
 * no field that could hold one, and
 * `backend/tests/test_router_tenant.py` sweeps the router's own OpenAPI
 * schema to prove the same thing on the server side.
 *
 * The secret is hashed, not encrypted (plan §3.2), so there is no "show
 * it to me again" to build. Anything in this bundle that caches, stores
 * or re-renders a `DeviceSecret` after the user has dismissed it is
 * building a second, worse copy of a thing the database deliberately
 * does not have.
 *
 * ## `credential_origin`, and why a read says anything about the secret
 *
 * Three values, decided by the server from the stored hash and the
 * hashers that verify it:
 *
 * - `issued` — we hashed it, so a plaintext existed once and was shown
 *   once.
 * - `migrated` — a bcrypt hash from the old service. **We never held the
 *   plaintext and never will.** The interface says that sentence rather
 *   than rendering an empty field, because an empty field reads as a
 *   bug and this is not one.
 * - `unrecognised` — no hasher recognises it, so it cannot authenticate
 *   either. Rendering it as a working device would be a lie.
 */
import { queryOptions } from '@tanstack/react-query';

import { apiGet, apiSend } from './http';

export type CredentialOrigin = 'issued' | 'migrated' | 'unrecognised';

export interface Device {
  id: number;
  name: string;
  /** The HTTP Basic username the router sends. Half of a credential
   *  pair and useless without the other half, and the owner has to be
   *  able to read it back to configure a replacement router. */
  username: string;
  created_at: string;
  last_seen_at: string | null;
  /** `null` means *inherit the namespace default*, which is not `0`
   *  (may never call). Two states, carried as two. */
  rate_limit_per_minute: number | null;
  /** What the limiter will actually allow, with `null` already resolved
   *  against the installation default.
   *
   *  Computed on the server by `effective_rate_limit` — the same
   *  function `/nic/update` calls on the request path. The browser must
   *  not resolve `null` itself: the installation default lives behind
   *  `app_setting.manage`, which a plain tenant does not hold, so a
   *  client-side resolution would either show nothing or invent a
   *  number. The one on screen is the one enforced, by construction. */
  effective_rate_limit_per_minute: number;
  credential_origin: CredentialOrigin;
  hostname_count: number;
}

/** A device **and its secret**. Returned by create and rotate, and by
 *  nothing else, ever. */
export interface DeviceSecret {
  device: Device;
  secret: string;
}

export const DEVICES_QUERY_KEY = ['atrium_ddns', 'devices'] as const;

/** The same permission the board reads under — a user who can see a
 *  device must be able to find the page that created it. */
export const DEVICE_PERMISSION = 'atrium_ddns.device.manage';

export async function getDevices(): Promise<Device[]> {
  return apiGet<Device[]>('/atrium_ddns/devices');
}

export function devicesQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: DEVICES_QUERY_KEY,
    queryFn: getDevices,
    enabled: options.enabled,
  });
}

/** One device, for `/atrium-ddns/devices/:id` (#89).
 *
 * Its own request rather than a `find` over the list, because the
 * reason the detail route exists is that *the list does not scale*
 * (`ui-design.md` §11.2) and a detail page that fetched every device to
 * display one would have moved the same `SELECT *` behind a narrower
 * URL.
 */
export async function getDevice(id: number): Promise<Device> {
  return apiGet<Device>(`/atrium_ddns/devices/${id}`);
}

/** Its own query key, per id.
 *
 * Deliberately *not* a slice of `DEVICES_QUERY_KEY`: a detail page that
 * read out of the list's cache would render nothing until the list had
 * been fetched, which on a linked URL pasted into a ticket is the whole
 * arrival path.
 */
export function deviceQuery(id: number | null, options: { enabled: boolean }) {
  return queryOptions({
    queryKey: [...DEVICES_QUERY_KEY, id] as const,
    queryFn: () => getDevice(id as number),
    enabled: options.enabled && id !== null,
  });
}

export async function createDevice(body: {
  name: string;
  rate_limit_per_minute?: number | null;
}): Promise<DeviceSecret> {
  return apiSend<DeviceSecret>('/atrium_ddns/devices', 'POST', body);
}

/** Change one device's rate limit. **Nothing else, and not the secret.**
 *
 * #73's route. Before it existed the only way to tighten a device's
 * limit was delete-and-recreate, which mints a new username and a new
 * secret — so the operator's only route to slowing an abusive device
 * was to break it until its owner reconfigured the router.
 *
 * `null` is a *value* here and means *inherit the installation
 * default*; `0` means *may never call*. The server requires the key to
 * be present for exactly that reason: an omitted key and an explicit
 * `null` would otherwise be the same request, and one of the two
 * readings un-mutes a device somebody muted on purpose.
 *
 * Returns the device as a **read** model — `Device`, not
 * `DeviceSecret`. There is no field on it that could carry a secret.
 */
export async function updateDeviceLimit(body: {
  id: number;
  rate_limit_per_minute: number | null;
}): Promise<Device> {
  return apiSend<Device>(`/atrium_ddns/devices/${body.id}`, 'PATCH', {
    rate_limit_per_minute: body.rate_limit_per_minute,
  });
}

/** Rename one device. **Still not the secret.**
 *
 * #89. `ui-design.md` §11.1: the conflict is `uq_ddns_device_user_name`
 * — `UNIQUE(user_id, name)` — and the server answers `409` with the
 * offending name in it. Nothing here catches that and retries with a
 * suffix; the refusal is the server's words and the form renders them
 * verbatim, which is the same rule `HostnameList` follows for the
 * zone-containment refusal.
 *
 * **`rate_limit_per_minute` is sent, and it must be the device's
 * *stored* value.** The server requires the key (#73: `null` is a
 * value, so an omitted key and an explicit `null` would be one request
 * and one of the two readings un-mutes a device somebody muted on
 * purpose). That leaves the rename with an obligation rather than a
 * choice: it has to re-send what is already stored, and sending
 * `effective_rate_limit_per_minute` instead would silently pin an
 * inheriting device to today's installation default — a rename that
 * quietly stops a device following a setting is exactly the class of
 * change #73 built this route to avoid.
 *
 * Returns the **read** model. There is no field on it that could carry
 * a secret.
 */
export async function renameDevice(body: {
  id: number;
  name: string;
  /** The stored value, `null` for *inherit*. Not the effective one. */
  rate_limit_per_minute: number | null;
}): Promise<Device> {
  return apiSend<Device>(`/atrium_ddns/devices/${body.id}`, 'PATCH', {
    name: body.name,
    rate_limit_per_minute: body.rate_limit_per_minute,
  });
}

export async function rotateDeviceSecret(id: number): Promise<DeviceSecret> {
  return apiSend<DeviceSecret>(`/atrium_ddns/devices/${id}/rotate`, 'POST');
}

export async function deleteDevice(id: number): Promise<void> {
  await apiSend<void>(`/atrium_ddns/devices/${id}`, 'DELETE');
}
