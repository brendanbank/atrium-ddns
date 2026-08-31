/** Log payloads for the component tests.
 *
 * Same rule as `fixtures.ts`: every field is a *decided* value, exactly
 * as `router.py` ships it. The builders never compute `cross_tenant`
 * from a permission list, never derive `any_rows_in_scope` from
 * `rows.length`, and never fold `success_response_codes` into a
 * literal — because the components under test do none of those either,
 * and a fixture that derived them would be a second implementation of
 * the rule and would make the tests agree with a bug.
 *
 * The addresses are the real shape M2 measured: 39 characters, eight
 * groups, seven colons, never `::`-compressed. `2001:db8::1` is eleven
 * characters and is what every other fixture in this repo uses, which is
 * exactly why it is not used here — a column sized against the short
 * form is wrong against production data.
 */
import type {
  EventFilters,
  EventPage,
  EventRow,
  EventVocabulary,
} from '../api/events';

export const V6_CALLER = '2001:0db8:0000:0000:0000:0000:0000:0001';
export const V6_OTHER = '2001:0db8:0000:0000:0000:0000:0000:0099';
export const V4_DECLARED = '192.0.2.9';

/** The vocabulary as the server derives it — three event types (the
 *  three with a writer), the wire's response codes, the provider
 *  registry's names, and the null sentinel. */
export function vocabulary(
  overrides: Partial<EventVocabulary> = {},
): EventVocabulary {
  return {
    event_types: ['auth', 'delete', 'update'],
    response_codes: [
      '911',
      'abuse',
      'badauth',
      'dnserr',
      'good',
      'nochg',
      'nohost',
      'notfqdn',
    ],
    backend_types: ['aws', 'cloudflare'],
    backend_type_none: '__none__',
    success_response_codes: ['good', 'nochg'],
    ...overrides,
  };
}

export function filters(overrides: Partial<EventFilters> = {}): EventFilters {
  return {
    user_id: null,
    device_id: null,
    domain_id: null,
    hostname_id: null,
    event_type: null,
    response_code: null,
    backend_type: null,
    client_ip: null,
    since: null,
    until: null,
    ...overrides,
  };
}

export function row(overrides: Partial<EventRow> = {}): EventRow {
  return {
    id: 1,
    created_at: '2026-08-15T14:02:00Z',
    user_id: 1,
    user_email: 'tenant@example.com',
    device_id: 7,
    device_name: 'roof-ap',
    domain_id: 3,
    domain_name: 'example.net',
    hostname_id: 9,
    hostname: 'host-a.example.net',
    event_type: 'update',
    response_code: 'nochg',
    client_ip: V6_CALLER,
    ip: V6_CALLER,
    backend_type: 'aws',
    message: null,
    ...overrides,
  };
}

export function page(overrides: Partial<EventPage> = {}): EventPage {
  return {
    rows: [row()],
    next_cursor: null,
    limit: 100,
    filters: filters(),
    vocabulary: vocabulary(),
    retention_days: 30,
    cross_tenant: false,
    // `null` is the honest default: the server measures this only when
    // a page comes back empty, and the default page has a row.
    any_rows_in_scope: null,
    unmatchable_filters: [],
    // `null` is *not asked*, and it is the honest default for the same
    // reason: the server measures this only when the caller filtered on
    // a partially-attributed response code. A default of `{ rows: 0 }`
    // would make every fixture assert a measurement nobody took.
    unattributable: null,
    ...overrides,
  };
}
