/** `/api/atrium_ddns/hostnames` — the writer #69 found was missing.
 *
 * ## The one thing this module must not grow
 *
 * **No hostname validation lives here.** Not a regex, not a
 * "must end with the zone" check, not a friendly pre-flight that saves a
 * round trip. The server decides, using the same two functions
 * `/nic/update` uses (`BaseProvider.isvalidhostname` and
 * `zone_contains`), and its refusal is rendered verbatim.
 *
 * A validator here would be a third implementation of a rule that two
 * code paths already share, and it would be the one nobody tests against
 * the wire. Its failure mode is silent and asymmetric: a name this
 * bundle rejects and `/nic/update` accepts reads as the interface lying,
 * and a name it accepts and the server refuses is a form that submits
 * and bounces. The server round trip is cheap; a second opinion about
 * DNS syntax is not.
 *
 * The one transformation the form does apply is `trim()`, and it is
 * applied *visibly* — the create form shows the exact string it will
 * send. The API deliberately does not strip, because stripping would
 * make it accept a byte sequence the wire refuses; trimming is a form
 * concern and belongs where the user can see it.
 *
 * ## `device_id` is nullable, and `null` is not "unset"
 *
 * A hostname may exist before it is assigned to anything, and outlives
 * the device it pointed at (`ON DELETE SET NULL`). So `null` is a value
 * the UI has to be able to *send*, not merely receive — `assignDevice`
 * takes `number | null` and always sends the key.
 */
import { queryOptions } from '@tanstack/react-query';

import { apiGet, apiSend } from './http';

export interface Hostname {
  id: number;
  name: string;
  domain_id: number;
  domain_name: string;
  /** `null` means *registered, not assigned* — a supported state, not a
   *  missing one. */
  device_id: number | null;
  /** `null` whenever `device_id` is. Resolved server-side through the
   *  tenancy scope, so a row pointing at a device this caller cannot see
   *  renders as `null` rather than leaking another tenant's device
   *  name. */
  device_name: string | null;
  created_at: string;
  /** What was last successfully published, and when. All three stay
   *  `null` until a `good` aggregate lands — a `nochg` leaves them
   *  untouched — so `null` means *nothing has ever been published*, not
   *  *the address is zero*. */
  last_ip_v4: string | null;
  last_ip_v6: string | null;
  last_updated_at: string | null;
}

export const HOSTNAMES_QUERY_KEY = ['atrium_ddns', 'hostnames'] as const;

/** The permission all four routes gate on. One string, so the UI's gate
 *  and the API's gate cannot drift. */
export const HOSTNAME_PERMISSION = 'atrium_ddns.hostname.manage';

export async function getHostnames(): Promise<Hostname[]> {
  return apiGet<Hostname[]>('/atrium_ddns/hostnames');
}

export function hostnamesQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: HOSTNAMES_QUERY_KEY,
    queryFn: getHostnames,
    enabled: options.enabled,
  });
}

export async function createHostname(body: {
  name: string;
  domain_id: number;
  device_id?: number | null;
}): Promise<Hostname> {
  return apiSend<Hostname>('/atrium_ddns/hostnames', 'POST', body);
}

/** Assign, reassign or unassign. `null` unassigns, and the key is always
 *  sent — the endpoint requires it, so "omitted" cannot be mistaken for
 *  "cleared". */
export async function assignDevice(
  id: number,
  deviceId: number | null,
): Promise<Hostname> {
  return apiSend<Hostname>(`/atrium_ddns/hostnames/${id}`, 'PATCH', {
    device_id: deviceId,
  });
}

export async function deleteHostname(id: number): Promise<void> {
  await apiSend<void>(`/atrium_ddns/hostnames/${id}`, 'DELETE');
}
