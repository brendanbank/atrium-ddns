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
import type { BoardHostname } from '../api/board';
import { LogLink } from '../LogSearchPage';
import { namesHrefForName } from '../paths';
import { ResolutionStrip, StripSkeleton } from './ResolutionStrip';

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
        <a
            className="ddns-data"
            href={namesHrefForName(hostname.id)}
            data-testid={`hostname-${hostname.name}-link`}
          >
            {hostname.name}
          </a>
        {/* A real state, not an empty one: #17 counts this slice as
            `hostnames_never_written` rather than dropping it. Two empty
            rails would be the lie. */}
        <span className="ddns-note">
            Nothing published yet — no strip to draw.
          </span>
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
          hostnameHref={namesHrefForName(hostname.id)}
          strip={strip}
        />
      ))}
      {logLink}
    </div>
  );
}

/** `DeviceBlock` and `DeviceBoard` lived here and are gone.
 *
 * The board is a flat table now — `BoardTable`, one row per (device,
 * name, family) — so the per-device block, its disclosure and the
 * separate section for unassigned names all went with it. What
 * survives in this file is what the *device card* still draws:
 * `HostnameBlock` (the strips for one name) and `BoardSkeleton`.
 *
 * The legend those blocks carried — the updates window and the
 * "nothing has been checked yet" line — moved into `BoardTable`
 * rather than being dropped: both numbers are read from the payload,
 * and an operator who changes `health_check_interval_minutes` must
 * not be able to make the sentence wrong.
 */

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
