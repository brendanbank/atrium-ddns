/** The board as one flat table: a row per name, per address family.
 *
 * ## Why this replaced the nested layout
 *
 * The board was device → names → strips, three levels deep, each strip a
 * card of three station rows. It read well with two devices and became
 * unusable at fifteen: the operator reported that the shape changes as
 * results arrive, which is the real objection — a device with a check
 * result and one without were laid out differently, so the eye had to
 * re-find the columns on every device.
 *
 * A table has one shape. Twenty names are twenty rows whether or not
 * anything has been checked, and the columns stay where they were.
 *
 * ## What survives from §3–§4
 *
 * The *facts* are unchanged and so is the rule that matters: **agreement
 * has no colour** (§1.2), so a healthy row is plain and only a real
 * divergence is accented. What is gone is the rail and the three-row
 * station stack — a drawing that cost thirty lines to say what three
 * columns say, and which only earned that cost when a page held two
 * devices.
 *
 * `answered` / `published` / `called from` are now columns rather than
 * stations, and the joint verdicts become the row's tone rather than
 * segments between stations.
 */
import type { Board, BoardDevice, BoardHostname, Strip } from "../api/board";
import { LogLink } from "../LogSearchPage";
import { absoluteTitle, formatAge } from "./format";
import { namesHrefForName } from "../paths";

/** One line of the table. A name with no strips still gets a row —
 *  "nothing published yet" is a state, not an absence, and dropping it
 *  would hide exactly the names nobody is updating. */
interface Row {
  device: BoardDevice | null;
  /** `null` for a device that has no names yet — see `flatten`. Exactly
   *  one of `device` and `hostname` may be null; a row with neither is
   *  not constructible. */
  hostname: BoardHostname | null;
  strip: Strip | null;
}

function flatten(board: Board): Row[] {
  const rows: Row[] = [];
  for (const device of board.devices) {
    // A device with no names still gets a row. It is the device you just
    // registered and collected a username for, and dropping it because
    // the table is keyed on names would leave the board silent about the
    // one thing you had just done.
    if (device.hostnames.length === 0) {
      rows.push({ device, hostname: null, strip: null });
    }
    for (const hostname of device.hostnames) {
      if (hostname.strips.length === 0)
        rows.push({ device, hostname, strip: null });
      for (const strip of hostname.strips)
        rows.push({ device, hostname, strip });
    }
  }
  // Names nobody updates, at the end and with no device. Kept in the same
  // table rather than a section of their own: they are the same object
  // and a second layout is the thing this rewrite removes.
  for (const hostname of board.unassigned_hostnames) {
    if (hostname.strips.length === 0)
      rows.push({ device: null, hostname, strip: null });
    for (const strip of hostname.strips)
      rows.push({ device: null, hostname, strip });
  }
  return rows;
}

/** The row's tone. `diverged` on either joint is the only thing that
 *  earns the accent — §1.2 Rule 1, unchanged. */
function toneOf(strip: Strip | null): "diverged" | "quiet" | "plain" {
  if (strip === null) return "quiet";
  if (strip.upper_joint === "diverged" || strip.lower_joint === "diverged") {
    return "diverged";
  }
  return "plain";
}

function answeredText(strip: Strip | null): string {
  if (strip === null) return "nothing published";
  const { address, status } = strip.answered;
  if (address) return address;
  // The status vocabulary verbatim — `missing`, `never_checked`,
  // `lookup_failed` are different facts and collapsing them to a dash
  // is the "four states in one type" mistake §4.2 is written against.
  return status.replace(/_/g, " ");
}

/** True when every strip on the board has never been checked — and there
 *  is at least one, so a board with no names does not claim the health
 *  check is behind. §4.5's fourth empty state. */
function nothingChecked(board: Board): boolean {
  const strips = board.devices.flatMap((device) =>
    device.hostnames.flatMap((hostname) => hostname.strips),
  );
  return (
    strips.length > 0 &&
    strips.every((strip) => strip.answered.status === "never_checked")
  );
}

export function BoardTable({
  board,
  onOpenDevice,
}: {
  board: Board;
  onOpenDevice: (id: number) => void;
}) {
  const rows = flatten(board);
  if (rows.length === 0) {
    return (
      <span className="ddns-note" data-testid="board-empty">
        You have no devices yet. Add one to get a DDNS username and password,
        then register a name for it to update.
      </span>
    );
  }
  return (
    <>
      {nothingChecked(board) ? (
        <p className="ddns-note" data-testid="board-never-checked">
          Nothing has been checked yet. The health check runs every{" "}
          {board.health_check_interval_minutes} minutes.
        </p>
      ) : null}
      <div className="ddns-boardtable" data-testid="board-table">
        <div className="ddns-boardtable__head">
          <span className="ddns-th" />
          <span className="ddns-th">Device</span>
          <span className="ddns-th">Name</span>
          <span className="ddns-th">Family</span>
          <span className="ddns-th">Answered</span>
          <span className="ddns-th">Published</span>
          <span className="ddns-th">Called from</span>
          <span className="ddns-th">Checked</span>
          {/* The device half of §0's question — "which of my devices
            stopped talking" — which the first flat draft dropped. A
            board that answers only the name half answers half the
            question it exists for. */}
          <span className="ddns-th">Last seen</span>
          <span className="ddns-th" data-testid="board-updates-head">
            Updates / {board.window_days} d
          </span>
          <span className="ddns-th" />
        </div>
        {rows.map((row, index) => {
          const tone = toneOf(row.strip);
          const family = row.strip?.family ?? "none";
          const key = row.hostname
            ? `h${row.hostname.id}-${family}`
            : `d${row.device?.id}`;
          return (
            <div
              key={key}
              className="ddns-boardtable__row"
              data-stripe={index % 2 === 1 ? "on" : "off"}
              data-tone={tone}
              data-testid={
                row.hostname
                  ? `board-row-${row.hostname.name}-${family}`
                  : `board-row-device-${row.device?.name}`
              }
            >
              {/* The marker column. Present on every row so the accent does
                not shift the grid when it appears. */}
              <span className="ddns-boardtable__mark" aria-hidden="true">
                {tone === "diverged" ? "!" : ""}
              </span>
              {row.device ? (
                <button
                  type="button"
                  className="ddns-data ddns-boardtable__device"
                  onClick={() => onOpenDevice(row.device!.id)}
                  data-testid={`board-open-${row.device.name}`}
                >
                  {row.device.name}
                </button>
              ) : (
                <span className="ddns-cell" data-tone="quiet">
                  no device
                </span>
              )}
              {row.hostname ? (
                <a
                  className="ddns-data"
                  href={namesHrefForName(row.hostname.id)}
                >
                  {row.hostname.name}
                </a>
              ) : (
                <span className="ddns-cell" data-tone="quiet">
                  no names yet
                </span>
              )}
              <span className="ddns-cell">{row.strip?.family ?? "—"}</span>
              <span className="ddns-cell">{answeredText(row.strip)}</span>
              <span className="ddns-cell">
                {row.strip?.published.address ?? "—"}
              </span>
              <span className="ddns-cell">
                {row.strip?.called_from.address ?? "—"}
              </span>
              <span
                className="ddns-cell"
                title={absoluteTitle(row.strip?.answered.checked_at ?? null)}
              >
                {formatAge(row.strip?.answered.checked_at ?? null)}
              </span>
              <span
                className="ddns-cell"
                title={absoluteTitle(row.device?.last_seen_at ?? null)}
                data-testid={
                  row.device ? `device-${row.device.name}-last-seen` : undefined
                }
              >
                {row.device ? formatAge(row.device.last_seen_at) : "—"}
              </span>
              <span
                className="ddns-cell"
                data-testid={
                  row.device ? `device-${row.device.name}-updates` : undefined
                }
              >
                {/* `updates_display` and not a computed count: a device that
                  has never called and one that called zero times in the
                  window are different facts, and the server is the only
                  thing that knows which. */}
                {row.device ? row.device.updates_display : "—"}
              </span>
              {row.hostname ? (
                <LogLink
                  params={{ hostname_id: row.hostname.id }}
                  data-testid={`board-log-${row.hostname.name}`}
                >
                  log
                </LogLink>
              ) : (
                <span />
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
