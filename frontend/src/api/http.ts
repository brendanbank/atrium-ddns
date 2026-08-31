/** The one place this bundle talks to the network.
 *
 * Plain `fetch`, no axios. `credentials: 'include'` carries atrium's auth
 * cookie so authenticated routes behave exactly as they do from atrium's
 * own SPA. Build with `VITE_API_BASE_URL="/api"` (the Dockerfile's
 * default) so the bundle calls atrium's API namespace — every JSON route
 * lives under `/api/...` because the SPA owns un-prefixed URL space
 * (atrium issue #89).
 *
 * ## Why the error carries a status
 *
 * The board has to tell *refused* apart from *empty*, and a thrown
 * `Error('api 403: …')` makes that a string match. `ApiError.status` is
 * the number, so a component can branch on 403 without parsing a
 * message — and a message-shaped branch is the defect
 * `docs/ops/overnight-template.md` records under "apply it to error
 * messages too": one fault produced three different message texts across
 * three library builds, and a fix keyed on message text passed on the
 * workstation and drifted in the container.
 *
 * Issues #45 and #46 add their own modules beside this one and call
 * `apiGet` / `apiSend` rather than reaching for `fetch`.
 */

const apiBase =
  (import.meta as { env?: { VITE_API_BASE_URL?: string } }).env
    ?.VITE_API_BASE_URL ?? '/api';

/** A non-2xx response, with the status kept as a number. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string) {
    super(`api ${status}: ${body}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body);
  }
  // **A success with no body is a success.** `DELETE` answers `204 No
  // Content`, and `res.json()` on an empty body throws `Unexpected end
  // of JSON input` — which surfaced as *"That did not work"* over a
  // delete that had in fact worked. The row was gone, the list never
  // refreshed, and the modal stayed open because the mutation's
  // `onSuccess` never ran.
  //
  // Checked on the response rather than at each call site: every
  // `DELETE` in this client hits it, so a per-caller fix would be three
  // copies of one rule and a fourth endpoint away from the same bug.
  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T;
  }
  const text = await res.text();
  if (text === '') return undefined as T;
  return JSON.parse(text) as T;
}

/** GET `path` under the API base and parse JSON. */
export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase}${path}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  return jsonOrThrow<T>(res);
}

/** POST/PATCH/DELETE `path` under the API base and parse JSON. */
export async function apiSend<T>(
  path: string,
  method: 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${apiBase}${path}`, {
    method,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return jsonOrThrow<T>(res);
}
