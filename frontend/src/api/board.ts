/** `GET /api/atrium_ddns/board` — the device board and its strips.
 *
 * The types below mirror `atrium_ddns.router`'s Pydantic models one for
 * one. That duplication is deliberate and it is the *only* duplication
 * allowed on this surface: the shapes are restated, the **verdicts are
 * not**. Every state on this board — the five `DnsCheckStatus` values,
 * the four `Liveness` values, both joint verdicts, the collapse
 * denominator, the `!` marker, the device ordering — arrives computed.
 *
 * `docs/ops/ui-design.md` §4.2, third prohibition: *"Never compute the
 * five states in the frontend. They arrive from the API as the
 * `DnsCheckStatus` string. Two implementations of a five-state rule is
 * how five states become three."*
 *
 * So there is no `if (dnsIp === null)` anywhere under `src/board/`.
 * There is a switch over `status`, which is a different thing: it maps a
 * decided state onto a rendering, and if the backend ever grows a sixth
 * state the switch stops being exhaustive and `tsc` says so.
 */
import { queryOptions } from '@tanstack/react-query';

import { apiGet, apiSend } from './http';

/** #17's `DnsCheckStatus`, verbatim. Three of the five carry a null
 *  address and they are three different facts. */
export type DnsCheckStatus =
  | 'never_checked'
  | 'ok'
  | 'mismatch'
  | 'missing'
  | 'error';

/** #17's `Liveness`, verbatim. */
export type Liveness =
  | 'never_seen'
  | 'last_call_failed'
  | 'idle'
  | 'active';

/** One joint on the rail — ui-design.md §3.2's five renderings. */
export type JointVerdict =
  | 'agreed'
  | 'diverged'
  | 'not_measured_never'
  | 'not_measured_failed'
  | 'not_applicable';

/** Why the lower joint was or was not evaluated — §3.3. */
export type LowerJointReason =
  | 'evaluated'
  | 'no_device'
  | 'no_update_on_record'
  | 'declared_myip'
  | 'not_comparable';

export type Family = 'A' | 'AAAA';

export interface AnsweredStation {
  address: string | null;
  status: DnsCheckStatus;
  checked_at: string | null;
  error: string | null;
}

export interface PublishedStation {
  address: string | null;
  updated_at: string | null;
}

export interface CalledFromStation {
  address: string | null;
  seen_at: string | null;
  reason: LowerJointReason;
  declared_address: string | null;
}

export interface Strip {
  family: Family;
  answered: AnsweredStation;
  published: PublishedStation;
  called_from: CalledFromStation;
  upper_joint: JointVerdict;
  lower_joint: JointVerdict;
  joints_agreed: number;
  joints_compared: number;
  joints_not_applicable: number;
  joints_unmeasured: number;
  collapsible: boolean;
}

export interface BoardHostname {
  id: number;
  name: string;
  domain_name: string;
  device_id: number | null;
  strips: Strip[];
}

export interface BoardDevice {
  id: number;
  name: string;
  liveness: Liveness;
  marked: boolean;
  last_seen_at: string | null;
  last_response_code: string | null;
  updates_in_window: number | null;
  updates_display: string;
  window_days: number;
  hostnames: BoardHostname[];
}

export interface Board {
  generated_at: string;
  window_days: number;
  health_check_interval_minutes: number;
  devices: BoardDevice[];
  unassigned_hostnames: BoardHostname[];
}

export const BOARD_QUERY_KEY = ['atrium_ddns', 'board'] as const;

/** The permission the endpoint gates on. Exported so the UI's gate and
 *  the API's gate are one string rather than two that drift. */
export const BOARD_PERMISSION = 'atrium_ddns.device.manage';

export async function getBoard(): Promise<Board> {
  return apiGet<Board>('/atrium_ddns/board');
}

/** Shared query options. `enabled` is the caller's, because the board
 *  must not be fetched at all for a user who does not hold
 *  `BOARD_PERMISSION` — a 403 in the network tab on every page load is
 *  noise, and the refusal is already known client-side.
 *
 *  `refetchInterval` is deliberately absent. The health check runs on
 *  the server's own cadence (`health_check_interval_minutes`, shipped in
 *  the payload) and a board that polls faster than the data changes
 *  produces motion this design spent §3.7 removing. */
export function boardQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: BOARD_QUERY_KEY,
    queryFn: getBoard,
    enabled: options.enabled,
  });
}

/** `POST /api/atrium_ddns/health-checks/run` — #75, ui-parity §3.3 G3.
 *
 * The server's `HealthCheckRunOut`, restated field for field. The same
 * rule the `Board` types above follow applies here and is worth
 * restating because a summary is exactly the shape somebody would be
 * tempted to recompute: **the shapes are restated, the arithmetic is
 * not.** `ok + mismatch + missing + error === records_checked` is
 * asserted server-side by `HealthCheckSummary.assert_consistent` before
 * the response is built; nothing under `src/` re-derives it.
 *
 * Three fields exist so a zero can be read:
 * - `enabled` — `false` means the operator turned the check off, and
 *   every count below it is a refusal rather than a clean sweep.
 * - `hostnames_never_written` — names with nothing published, counted
 *   rather than silently dropped out of the denominator.
 * - `truncated` — the batch ceiling was reached, so there is more due
 *   work than this run did.
 */
export interface HealthCheckRun {
  enabled: boolean;
  /** Always `true` from this endpoint: a manual run is by definition
   *  not the scheduled one, and the flag says so rather than leaving a
   *  reader to infer it from the URL that produced the payload. */
  forced: boolean;
  hostnames_considered: number;
  hostnames_never_written: number;
  hostnames_checked: number;
  records_checked: number;
  ok: number;
  mismatch: number;
  missing: number;
  error: number;
  /** Names whose aggregate verdict changed. The number an operator who
   *  has just fixed something is actually looking for. */
  transitions: number;
  truncated: boolean;
  batch_size: number;
}

/** `POST /api/atrium_ddns/health-checks/clear`.
 *
 * `cleared` alone reads identically whether it reset the right rows or
 * every row in the installation, so the denominator travels with it.
 */
export interface HealthCheckClear {
  cleared: number;
  in_scope: number;
}

export async function runHealthChecks(): Promise<HealthCheckRun> {
  return apiSend<HealthCheckRun>('/atrium_ddns/health-checks/run', 'POST');
}

export async function clearHealthChecks(): Promise<HealthCheckClear> {
  return apiSend<HealthCheckClear>('/atrium_ddns/health-checks/clear', 'POST');
}
