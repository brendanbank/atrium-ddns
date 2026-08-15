/** The bundle's API surface, one module per resource.
 *
 * `./http` owns `fetch`, the base URL and `ApiError`. Everything else
 * imports from it. Issues #45 (domains, devices, credentials) and #46
 * (log search) add `./domains.ts`, `./devices.ts`, `./events.ts` beside
 * `./board.ts` and re-export here.
 */
export { ApiError, apiGet, apiSend } from './http';
export * from './state';
export * from './board';
