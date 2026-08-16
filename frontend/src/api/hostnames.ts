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

/* ------------------------------------------------------------------ *
 * Publishing — which backends, at what TTL, and "do it now" (#74)
 *
 * The last two legacy routes of this group. Three fields on the wire
 * that do not exist in the four calls above, because until `0004` there
 * was no schema for them.
 *
 * ## The one rule this half must not break
 *
 * **`selected` is not `published to`.** A hostname with no selection
 * rows publishes to *every* backend on its zone — an empty selection
 * means inherit, not "nowhere", and every name that existed before the
 * migration is in exactly that state. So the server ships both: the
 * stored `selected` flag per binding, and the resolved `publishes_to`
 * list. Rendering the first as if it were the second would tell an
 * owner that a working name publishes to nothing, on every screen, for
 * every name nobody has ever edited.
 *
 * The bundle therefore computes neither. `inherits_backends`,
 * `publishes_to` and `effective_ttl` all arrive decided, for the reason
 * `ui-design.md` §4.2 gives about the five `DnsCheckStatus` states: two
 * implementations of a fallback chain is how a three-level fallback
 * becomes a two-level one.
 * ------------------------------------------------------------------ */

export interface HostnameBackendChoice {
  backend_id: number;
  backend_type: string;
  /** Whether a selection row exists. **Not** "is published to" — see
   *  the note above. With an empty selection every entry is `false` and
   *  every entry is published to. */
  selected: boolean;
  credentials_set: boolean;
  /** The binding's own TTL, or `null` when it has none. */
  binding_ttl: number | null;
  /** What this name would be published at through this binding right
   *  now: override, else binding, else the service default. Decided
   *  server-side. */
  effective_ttl: number;
}

export interface HostnamePublishing {
  hostname_id: number;
  name: string;
  domain_id: number;
  domain_name: string;
  device_id: number | null;
  /** `true` when nothing is selected, i.e. this name follows its zone.
   *  A separate field rather than `selected.length === 0` because
   *  *inheriting everything* and *having chosen nothing* are the same
   *  rows and different sentences. */
  inherits_backends: boolean;
  /** The stored override. `null` is *inherit*, and it is not 60. */
  ttl: number | null;
  default_ttl: number;
  ttl_min: number;
  ttl_max: number;
  backends: HostnameBackendChoice[];
  /** Resolved backend ids in publish order — the order the aggregate's
   *  "first status that is neither good nor nochg" walks. */
  publishes_to: number[];
}

export interface BackendAttempt {
  backend_id: number;
  backend_type: string;
  /** The wire vocabulary: `good` | `nochg` | `nohost` | `dnserr` |
   *  `911`. One vocabulary for the whole service. */
  status: string;
}

export interface ManualUpdateResult {
  hostname_id: number;
  name: string;
  /** The *normalised* address, which may differ from what was typed. */
  ip: string;
  rtype: string;
  status: string;
  attempts: BackendAttempt[];
  published: boolean;
  rate_limit_per_minute: number;
  /** `worker_jobs.SUCCESS_RESPONSE_CODES`, shipped so the result panel
   *  can accent a non-success per-backend status without holding its
   *  own copy of the classification. Same list, same reason, as
   *  `EventVocabulary.success_response_codes`. */
  success_response_codes: string[];
}

export function publishingQueryKey(id: number) {
  return ['atrium_ddns', 'hostnames', id, 'publishing'] as const;
}

export async function getPublishing(id: number): Promise<HostnamePublishing> {
  return apiGet<HostnamePublishing>(`/atrium_ddns/hostnames/${id}/backends`);
}

export function publishingQuery(id: number | null) {
  return queryOptions({
    queryKey: publishingQueryKey(id ?? 0),
    queryFn: () => getPublishing(id as number),
    enabled: id !== null,
  });
}

/** Replace the whole publishing configuration.
 *
 * `PUT`, and both keys are always sent — the endpoint requires them, so
 * "omitted" cannot be mistaken for "cleared". `backend_ids: []` and
 * `backend_ids: null` are the same request and both restore
 * inheritance; the response says which it did. */
export async function setPublishing(
  id: number,
  body: { backend_ids: number[] | null; ttl: number | null },
): Promise<HostnamePublishing> {
  return apiSend<HostnamePublishing>(
    `/atrium_ddns/hostnames/${id}/backends`,
    'PUT',
    body,
  );
}

/** Publish an address now, without waiting for the router.
 *
 * Charged against the device's rate-limit budget — the same one
 * `/nic/update` draws on, because both cost the provider the same. A
 * `429` here is a real refusal and its `detail` is rendered verbatim. */
export async function manualUpdate(
  id: number,
  ip: string,
): Promise<ManualUpdateResult> {
  return apiSend<ManualUpdateResult>(
    `/atrium_ddns/hostnames/${id}/update`,
    'POST',
    { ip },
  );
}
