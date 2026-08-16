/** `/api/atrium_ddns/domains`, `/backends` and `/providers`.
 *
 * The types mirror `atrium_ddns.router`'s Pydantic models one for one,
 * which is the same deliberate duplication `board.ts` makes and carries
 * the same limit: **the shapes are restated, the decisions are not.**
 *
 * Two decisions arrive computed and are never re-derived here:
 *
 * - `credentials_set` — whether a credential is stored. The server
 *   reads `credentials_ct IS NOT NULL` off the column without
 *   decrypting, so there is no cleartext anywhere on this path. There
 *   is deliberately no masked value, no length and no prefix to render:
 *   *"a prefix of an API token is still a disclosure and it is the shape
 *   people reach for first"* (`providers/base.py`).
 * - `credential_keys` — which fields a provider's form has. Derived
 *   server-side from `BaseProvider.REQUIRED_CREDENTIALS`, so a provider
 *   that gains a key grows the field the next time the page loads. A
 *   field list typed into TypeScript is the identical defect one release
 *   later.
 */
import { queryOptions } from '@tanstack/react-query';

import type { CredentialsPayload } from './credentials';
import { apiGet, apiSend } from './http';

export interface DomainBackend {
  id: number;
  domain_id: number;
  backend_type: string;
  config: Record<string, unknown>;
  /** Ciphertext present. Never the ciphertext, and never anything
   *  derived from the plaintext. */
  credentials_set: boolean;
  /** Whether this build ships an adapter for `backend_type`. A migrated
   *  row may name a service nobody claims; the wire half is `911`, and
   *  the interface says so rather than rendering it as though it
   *  worked. */
  known_service: boolean;
  credential_keys: string[];
}

export interface Domain {
  id: number;
  name: string;
  created_at: string;
  backends: DomainBackend[];
  hostname_count: number;
}

export interface Provider {
  service: string;
  credential_keys: string[];
}

export const DOMAINS_QUERY_KEY = ['atrium_ddns', 'domains'] as const;
export const PROVIDERS_QUERY_KEY = ['atrium_ddns', 'providers'] as const;

/** The permission both endpoints gate on. One string, so the UI's gate
 *  and the API's gate cannot drift. */
export const DOMAIN_PERMISSION = 'atrium_ddns.domain.manage';

export async function getDomains(): Promise<Domain[]> {
  return apiGet<Domain[]>('/atrium_ddns/domains');
}

export async function getProviders(): Promise<Provider[]> {
  const payload = await apiGet<{ providers: Provider[] }>(
    '/atrium_ddns/providers',
  );
  return payload.providers;
}

export function domainsQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: DOMAINS_QUERY_KEY,
    queryFn: getDomains,
    enabled: options.enabled,
  });
}

export function providersQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: PROVIDERS_QUERY_KEY,
    queryFn: getProviders,
    enabled: options.enabled,
    // The catalogue is a property of the build, not of a tenant, and it
    // cannot change without a deploy. Refetching it per mount would be
    // a request that can only ever return the same bytes.
    staleTime: Infinity,
  });
}

/** `POST /api/atrium_ddns/domains` — the zone and, #88, its first
 *  provider in one submission.
 *
 * `backend` is `null` for the "add a provider later" path and is sent
 * as an explicit `null` rather than omitted, so the request body says
 * which of the two shapes it is. The server creates both rows in one
 * transaction: a credential it refuses takes the zone with it, rather
 * than leaving behind a zone that answers `911` for every update under
 * it while the browser reports a failure.
 *
 * That atomicity is the reason this is one call and not two. Two calls
 * from here would have a half-succeeded state with no owner — and it is
 * exactly the state the design says must never be arrived at by
 * accident.
 */
export async function createDomain(input: {
  name: string;
  backend: BackendWrite | null;
}): Promise<Domain> {
  return apiSend<Domain>('/atrium_ddns/domains', 'POST', {
    name: input.name,
    backend: input.backend,
  });
}

/** `PATCH /api/atrium_ddns/domains/{id}` — the rename, #75 / §3.3 G4.
 *
 * `PATCH`, not `PUT`: `PUT` is still `405` and that is the server's
 * decision rather than an omission — every partial mutation on this
 * surface is a `PATCH`, and a second spelling of one operation is how a
 * divergent validation path starts.
 *
 * The server refuses (`409`) a rename that would leave any hostname
 * outside the zone, rather than rewriting the names. **Nothing here
 * pre-checks that**, deliberately: containment is decided by
 * `zone_contains`, the same function object `/nic/update` reaches, and
 * a second copy of the rule in TypeScript would be a copy that could
 * disagree with the wire. The form shows the server's refusal.
 */
export async function renameDomain(id: number, name: string): Promise<Domain> {
  return apiSend<Domain>(`/atrium_ddns/domains/${id}`, 'PATCH', { name });
}

export async function deleteDomain(id: number): Promise<void> {
  await apiSend<void>(`/atrium_ddns/domains/${id}`, 'DELETE');
}

export interface BackendWrite {
  backend_type: string;
  config: Record<string, unknown>;
  /** Built by `buildCredentialsPayload` and by nothing else. Typed as
   *  the union rather than as `Record<string, string> | undefined`
   *  precisely so `null` (clear) and `''` (preserve) cannot collapse
   *  into "not supplied" on the way out. */
  credentials: CredentialsPayload;
}

export async function createBackend(
  domainId: number,
  body: BackendWrite,
): Promise<DomainBackend> {
  return apiSend<DomainBackend>(
    `/atrium_ddns/domains/${domainId}/backends`,
    'POST',
    body,
  );
}

export async function updateBackend(
  backendId: number,
  body: { config?: Record<string, unknown>; credentials: CredentialsPayload },
): Promise<DomainBackend> {
  return apiSend<DomainBackend>(
    `/atrium_ddns/backends/${backendId}`,
    'PATCH',
    body,
  );
}

export async function deleteBackend(backendId: number): Promise<void> {
  await apiSend<void>(`/atrium_ddns/backends/${backendId}`, 'DELETE');
}
