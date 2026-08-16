/** The bundle's API surface, one module per resource.
 *
 * `./http` owns `fetch`, the base URL and `ApiError`. Everything else
 * imports from it. #46 (log search) adds `./events.ts` beside these and
 * re-exports here.
 *
 * `./credentials` is the odd one out and deliberately so: it is not a
 * resource, it is the **client half of the blank-preserves rule** — the
 * one place a credential form's state is turned into a wire value, so
 * that `""` cannot be produced by a text box being empty.
 */
export { ApiError, apiGet, apiSend } from './http';
export * from './state';
export * from './board';
export * from './credentials';
export * from './domains';
export * from './devices';
export * from './hostnames';
