/** The scaffold's demo endpoints. Kept because `router.py`, the home
 *  widget and `scripts/smoke.sh` all still reference them; they go when
 *  the demo widget does.
 */
import { apiGet, apiSend } from './http';

export interface AtriumDdnsState {
  message: string;
  counter: number;
}

export async function getAtriumDdnsState(): Promise<AtriumDdnsState> {
  return apiGet<AtriumDdnsState>('/atrium_ddns/state');
}

export async function bumpAtriumDdns(): Promise<AtriumDdnsState> {
  return apiSend<AtriumDdnsState>('/atrium_ddns/bump', 'POST');
}
