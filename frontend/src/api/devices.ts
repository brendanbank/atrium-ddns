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

export async function rotateDeviceSecret(id: number): Promise<DeviceSecret> {
  return apiSend<DeviceSecret>(`/atrium_ddns/devices/${id}/rotate`, 'POST');
}

export async function deleteDevice(id: number): Promise<void> {
  await apiSend<void>(`/atrium_ddns/devices/${id}`, 'DELETE');
}
