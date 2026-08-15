/** Plain fetch — no axios. `credentials: 'include'` carries atrium's
 *  auth cookie so authenticated routes work the same as from atrium's
 *  own SPA. Build with `VITE_API_BASE_URL="/api"` (the default in the
 *  Dockerfile) so the bundle calls atrium's API namespace — every
 *  JSON route lives under /api/... so the SPA owns un-prefixed URL
 *  space (atrium issue #89).
 */
const apiBase =
  (import.meta as { env?: { VITE_API_BASE_URL?: string } }).env?.VITE_API_BASE_URL ??
  '/api';

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`api ${res.status}: ${body}`);
  }
  return (await res.json()) as T;
}

export interface AtriumDdnsState {
  message: string;
  counter: number;
}

export async function getAtriumDdnsState(): Promise<AtriumDdnsState> {
  const res = await fetch(`${apiBase}/atrium_ddns/state`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  return jsonOrThrow<AtriumDdnsState>(res);
}

export async function bumpAtriumDdns(): Promise<AtriumDdnsState> {
  const res = await fetch(`${apiBase}/atrium_ddns/bump`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'application/json' },
  });
  return jsonOrThrow<AtriumDdnsState>(res);
}
