/** Board payloads for the component tests.
 *
 * Every field is a *decided* value, exactly as `router.py` ships it —
 * these builders never compute a verdict from an address, because the
 * component under test never does either. A fixture that derived
 * `upper_joint` from the two addresses would be a second implementation
 * of the rule and would make the tests agree with a bug.
 *
 * The addresses are the real shape M2 measured: 39 characters, eight
 * groups, seven colons, never `::`-compressed. `2001:db8::1` is eleven
 * characters and is what every other fixture in this repo uses, which is
 * exactly why it is not used here.
 */
import type {
  Board,
  BoardDevice,
  BoardHostname,
  Strip,
} from '../api/board';

export const V6_A = '2001:0db8:0000:0000:0000:0000:0000:0001';
export const V6_B = '2001:0db8:0000:0000:0000:0000:0000:0099';

export function strip(overrides: Partial<Strip> = {}): Strip {
  return {
    family: 'AAAA',
    answered: {
      address: V6_A,
      status: 'ok',
      checked_at: '2026-08-15T14:02:00Z',
      error: null,
    },
    published: { address: V6_A, updated_at: '2026-08-15T13:47:00Z' },
    called_from: {
      address: V6_A,
      seen_at: '2026-08-15T13:47:00Z',
      reason: 'evaluated',
      declared_address: null,
    },
    upper_joint: 'agreed',
    lower_joint: 'agreed',
    joints_agreed: 2,
    joints_compared: 2,
    joints_not_applicable: 0,
    joints_unmeasured: 0,
    collapsible: true,
    ...overrides,
  };
}

export function hostname(overrides: Partial<BoardHostname> = {}): BoardHostname {
  return {
    id: 1,
    name: 'host-a.example.net',
    domain_name: 'example.net',
    device_id: 1,
    strips: [strip()],
    ...overrides,
  };
}

export function device(overrides: Partial<BoardDevice> = {}): BoardDevice {
  return {
    id: 1,
    name: 'home-router',
    liveness: 'active',
    marked: false,
    last_seen_at: '2026-08-15T13:47:00Z',
    last_response_code: 'good',
    updates_in_window: 213,
    updates_display: '213',
    window_days: 7,
    hostnames: [hostname()],
    ...overrides,
  };
}

export function board(overrides: Partial<Board> = {}): Board {
  return {
    generated_at: '2026-08-15T14:05:00Z',
    window_days: 7,
    health_check_interval_minutes: 15,
    devices: [device()],
    unassigned_hostnames: [],
    ...overrides,
  };
}
