/** The device board — `docs/ops/ui-design.md` §3.6.
 *
 * A ledger ordered by liveness with no sort control in the default
 * view, because **the ordering is the opinion**. The rejected
 * alternative — a sortable table — makes the answer the *user's* job:
 * the board opens in whatever order it was last sorted, and "which one
 * is quiet" becomes a sort somebody has to know to perform. Here a
 * device that has gone quiet is at the top of the page without anyone
 * asking, and the order arrives from the server (`never_seen` →
 * `last_call_failed` → `idle` → `active`, oldest first inside a bucket)
 * so it cannot be re-derived into something else.
 *
 * ## The three strings that are three facts
 *
 * `; updates / N d` renders `updates_display`, which is
 * `DeviceStatus.render_updates()` on the server: `—` for a device that
 * has never called, `error` for one whose last call failed, and the
 * count — including a real `0` — for the rest. A renderer that formatted
 * `updates_in_window` directly would print `None`, or worse coerce it to
 * `0`, and a device that has never been heard from would become
 * indistinguishable from one that is simply quiet. The `N` in the header
 * is `window_days` from the same payload, transported beside the count
 * precisely so a caller cannot render the numerator without its
 * denominator.
 *
 * ## The marker
 *
 * `!` on `never_seen` and `last_call_failed` only, and the server
 * decides it (`marked`). `idle` is not marked and M3 is why: half the
 * fleet produced zero events in a 24-hour window, so marking idle would
 * paint half the board and destroy the marker. Idle is normal, and it
 * renders as a measured `0` — a statement, not a silence.
 */
import { useState } from 'react';

import type { Board, BoardDevice, BoardHostname } from '../api/board';
import { LogLink } from '../LogSearchPage';
import { ResolutionStrip, StripSkeleton } from './ResolutionStrip';
import { absoluteTitle, formatAge, rateLimitSummary } from './format';

/* The board answers *which* device stopped talking. The next question is
   always *when, and what did it say* — and that is the log, filtered to
   this row. #46's acceptance criterion is that the filters are
   "reachable pre-applied from any device or hostname row"; these two
   links are what makes that literal. The filter travels as the row's
   id, so a device whose name was reused does not collect a predecessor's
   history. */

/** One name and its strips.
 *
 * Exported for #89's device detail route, which renders the same block
 * at full width under `; names this device updates`. Reused rather than
 * reimplemented for `api/board.ts`'s own reason — the shapes may be
 * restated, the verdicts may not — and a second renderer of the
 * signature element is precisely where a sixth `DnsCheckStatus` would
 * get a default branch. */
export function HostnameBlock({ hostname }: { hostname: BoardHostname }) {
  const logLink = (
    <LogLink
      params={{ hostname_id: hostname.id }}
      data-testid={`hostname-${hostname.name}-log`}
    >
      log
    </LogLink>
  );
  if (hostname.strips.length === 0) {
    return (
      <div className="ddns-hostname__strips" data-testid={`hostname-${hostname.name}`}>
        <span className="ddns-data">{hostname.name}</span>
        {/* A real state, not an empty one: #17 counts this slice as
            `hostnames_never_written` rather than dropping it. Two empty
            rails would be the lie. */}
        <span className="ddns-label">nothing published yet — no strip to draw</span>
        {logLink}
      </div>
    );
  }
  return (
    <div className="ddns-hostname__strips" data-testid={`hostname-${hostname.name}`}>
      {hostname.strips.map((strip) => (
        <ResolutionStrip
          key={`${hostname.id}-${strip.family}`}
          hostname={hostname.name}
          strip={strip}
        />
      ))}
      {logLink}
    </div>
  );
}

function DeviceBlock({
  device,
  windowDays,
}: {
  device: BoardDevice;
  windowDays: number;
}) {
  // A device collapses only when everything under it agrees. Same rule
  // as §3.4's strips, applied one level up: nothing that hides a
  // divergence may be the default, and "page height is an instrument"
  // only holds if a healthy device is short.
  const anythingWrong =
    device.marked ||
    device.hostnames.some(
      (hostname) =>
        hostname.strips.length === 0 ||
        hostname.strips.some((strip) => !strip.collapsible),
    );
  const [expanded, setExpanded] = useState(anythingWrong);

  return (
    <section data-testid={`device-${device.name}`} data-liveness={device.liveness}>
      <button
        type="button"
        className="ddns-device__line"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className="ddns-device__marker" aria-hidden="true">
          {device.marked ? '!' : ''}
        </span>
        <span className="ddns-data">
          {device.name}
          {device.marked ? (
            <span className="ddns-sr"> — needs attention</span>
          ) : null}
        </span>
        <span
          className="ddns-station__time"
          title={absoluteTitle(device.last_seen_at)}
          data-testid={`device-${device.name}-last-seen`}
        >
          {formatAge(device.last_seen_at)}
        </span>
        <span
          className="ddns-station__time"
          data-testid={`device-${device.name}-updates`}
        >
          {device.updates_display}
        </span>
      </button>
      {expanded ? (
        <div className="ddns-device__hostnames">
          <LogLink
            params={{ device_id: device.id }}
            data-testid={`device-${device.name}-log`}
          >
            log for this device
          </LogLink>
          {/* #73's AC 4 — the stored limit is displayed wherever a
              device is shown. In the *detail*, not in the line: the
              board's four columns are a status grid and §4 spends its
              boldness on the strip. This is configuration sitting
              quietly beside the log link, which is where a reader who
              has already asked "what is wrong with this device" looks
              next. */}
          <span
            className="ddns-label"
            data-testid={`device-${device.name}-limit`}
          >
            rate limit {rateLimitSummary(device)}
          </span>
          {device.hostnames.length === 0 ? (
            <span className="ddns-label">
              this device has no hostnames. Assign one to start tracking it.
            </span>
          ) : (
            device.hostnames.map((hostname) => (
              <HostnameBlock key={hostname.id} hostname={hostname} />
            ))
          )}
        </div>
      ) : null}
      <span className="ddns-sr">
        {device.updates_display} updates in the last {windowDays} days
      </span>
    </section>
  );
}

export interface DeviceBoardProps {
  board: Board;
}

export function DeviceBoard({ board }: DeviceBoardProps) {
  if (board.devices.length === 0 && board.unassigned_hostnames.length === 0) {
    return (
      <p data-testid="board-empty">
        You have no devices yet. Add one to get a DDNS username and password.
      </p>
    );
  }

  // §4.5's fourth empty state. The interval is read from the payload,
  // never typed into the sentence — an operator who changes
  // `health_check_interval_minutes` must not be able to make this string
  // wrong.
  const nothingChecked =
    board.devices.every((device) =>
      device.hostnames.every((hostname) =>
        hostname.strips.every(
          (strip) => strip.answered.status === 'never_checked',
        ),
      ),
    ) &&
    board.devices.some((device) =>
      device.hostnames.some((hostname) => hostname.strips.length > 0),
    );

  return (
    <div className="ddns-board" data-testid="board">
      <div className="ddns-board__head">
        <span aria-hidden="true" />
        <span className="ddns-label">device</span>
        <span className="ddns-label">last seen</span>
        <span className="ddns-label" data-testid="board-updates-head">
          updates / {board.window_days} d
        </span>
      </div>
      {nothingChecked ? (
        <p data-testid="board-never-checked">
          Nothing has been checked yet. The health check runs every{' '}
          {board.health_check_interval_minutes} minutes.
        </p>
      ) : null}
      {board.devices.map((device) => (
        <DeviceBlock
          key={device.id}
          device={device}
          windowDays={board.window_days}
        />
      ))}
      {board.unassigned_hostnames.length > 0 ? (
        <section data-testid="board-unassigned">
          <span className="ddns-label">
            hostnames with no device — nothing can update these
          </span>
          <div className="ddns-device__hostnames">
            {board.unassigned_hostnames.map((hostname) => (
              <HostnameBlock key={hostname.id} hostname={hostname} />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

/** §4.5's loading state, at board scale. Static grey blocks, no
 *  shimmer, and critically **no rail** — a loading strip that carried
 *  one could be mistaken for an agreed one. */
export function BoardSkeleton() {
  return (
    <div className="ddns-board" data-testid="board-loading">
      <StripSkeleton />
      <StripSkeleton />
    </div>
  );
}
