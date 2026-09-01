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
import { useState } from "react";
import {
  ActionIcon,
  Button,
  Group,
  Select,
  Tooltip,
} from "@mantine/core";
import { IconListSearch, IconPlus } from "@tabler/icons-react";

import type { Board, BoardDevice, BoardHostname, Strip } from "../api/board";
import { LogLink } from "../LogSearchPage";
import { absoluteTitle, formatAge } from "./format";
import { boardNameHref, boardNameNewHref } from "../paths";

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

/** The board's view filters, as one value. Each is the selected option or
 *  `null` for "any" — the shape Mantine's clearable `Select` already
 *  speaks, so no filter needs a sentinel of its own.
 *
 *  Adding one here is a compile error at `NO_FILTERS` until it is given a
 *  default, which is the point: `filtered` and the clear control are both
 *  derived from this record, and a filter that exists in only one of them
 *  is #141. */
interface ViewFilters {
  device: string | null;
  name: string | null;
  zone: string | null;
}

/** Every filter off. Both the initial state's base and what the clear
 *  control resets to — deliberately one value, so "cleared" cannot come
 *  to mean two different things. */
const NO_FILTERS: ViewFilters = { device: null, name: null, zone: null };

export function BoardTable({
  board,
  onOpenDevice,
  initialZoneFilter = null,
  initialDeviceFilter = null,
}: {
  board: Board;
  onOpenDevice: (id: number) => void;
  /** From `?zone=` on this page's own address. */
  initialZoneFilter?: string | null;
  /** From `?onlyDevice=` — the device card links here to show the rows it
   *  used to draw itself. */
  initialDeviceFilter?: string | null;
}) {
  /* This component holds no mutation. It draws rows and filters them;
     every write a row can reach — delete, rename, rotate, add a name —
     belongs to the card or the form the row links to. #155 removed the
     one exception, a `deleteDevice` mutation driven by a trash icon in
     the device cell, together with the query invalidation and the error
     state that existed only to serve it. */

  /** The view filters over the rows already on screen. `zone` is seeded
   *  from `?zone=` so the zone list can link here focused on one zone —
   *  it used to link to `/atrium-ddns/names?zone=`, a page that is going
   *  away — and `device` from `?onlyDevice=` for the device card. Both
   *  are read once, as initial state; changing a filter afterwards does
   *  not push history, because it is a question about what is in front of
   *  you rather than a different query.
   *
   *  Deliberately client-side. The board is one request that already
   *  carries every device and name this tenant has. And unlike a zone or
   *  a name, "I was looking at one device" is not a thing anyone pastes
   *  into a ticket — the log search is where a shareable filtered view
   *  lives, and each row links straight into it.
   *
   *  **One record rather than one `useState` per filter, and that is the
   *  fix for #141.** As three separate hooks, `filtered` was computed
   *  over all three and `clear` reset two of them, so the zone filter —
   *  the one a zones-list link seeds, and therefore the one most likely
   *  to be the only filter set — survived its own clear button. A tenant
   *  arriving at a zone with no names read *"clear the filter to see
   *  them"*, pressed clear, and nothing happened: the empty-state fix's
   *  instruction was the one instruction the surface could not follow.
   *
   *  Here `filtered` and `clearFilters` are both derived from the keys of
   *  the same record, and `NO_FILTERS` is typed as a whole `ViewFilters`,
   *  so a fourth filter is a type error until it is added to both. The
   *  invariant is checked by `tsc` rather than kept by hand, which is
   *  what stops this defect regrowing. */
  const [filters, setFilters] = useState<ViewFilters>({
    ...NO_FILTERS,
    device: initialDeviceFilter,
    zone: initialZoneFilter,
  });
  const setFilter = (key: keyof ViewFilters) => (value: string | null) =>
    setFilters((current) => ({ ...current, [key]: value }));
  const clearFilters = () => setFilters(NO_FILTERS);

  const allRows = flatten(board);
  const rows = allRows.filter(
    (row) =>
      (filters.device === null || String(row.device?.id) === filters.device) &&
      (filters.name === null || String(row.hostname?.id) === filters.name) &&
      (filters.zone === null || row.hostname?.domain_name === filters.zone),
  );

  /** Options come from the board payload, so they can only offer things
   *  the table can actually show — a filter that selects nothing is a
   *  filter that should not have been offered. Sorted by name because the
   *  payload's order is publish order, which is meaningless in a picker. */
  const deviceOptions = board.devices
    .map((d) => ({ value: String(d.id), label: d.name }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const nameOptions = [
    ...board.devices.flatMap((d) => d.hostnames),
    ...board.unassigned_hostnames,
  ]
    .map((h) => ({ value: String(h.id), label: h.name }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const zoneOptions = Array.from(
    new Set(
      [
        ...board.devices.flatMap((d) => d.hostnames),
        ...board.unassigned_hostnames,
      ]
        .map((h) => h.domain_name)
        .filter((n): n is string => Boolean(n)),
    ),
  )
    .sort((a, b) => a.localeCompare(b))
    .map((name) => ({ value: name, label: name }));
  /** Over the record's own values, not over a list of names written out
   *  again here. This predicate and `clearFilters` are the pair that
   *  disagreed in #141; neither enumerates a filter now. */
  const filtered = Object.values(filters).some((value) => value !== null);
  /** The account is empty — a fact about the tenant.
   *
   *  Keyed on `allRows`, **not** on the filtered `rows`. Keying it on the
   *  filtered set made a filter that matched nothing render *"You have no
   *  devices yet"* over an account with twelve of them: two different
   *  facts in one string, and the more alarming one shown for the more
   *  ordinary cause. It also hid the filter controls, so the only way out
   *  was to reload. A filtered-empty result is a *measurement*, and it
   *  says so below, with the controls still on screen. */
  if (allRows.length === 0) {
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
      {/* Pick any of these and the table narrows to it. Searchable because
          a tenant with forty names should type rather than scroll, and
          clearable — individually here, all at once below — because
          removing a filter must be as easy as adding one. */}
      <Group gap="sm" align="flex-end" wrap="wrap" data-testid="board-filters">
        <Select
          label="Device"
          placeholder="any device"
          data={deviceOptions}
          value={filters.device}
          onChange={setFilter("device")}
          searchable
          clearable
          size="xs"
          w={200}
          data-testid="board-filter-device"
        />
        <Select
          label="Name"
          placeholder="any name"
          data={nameOptions}
          value={filters.name}
          onChange={setFilter("name")}
          searchable
          clearable
          size="xs"
          w={240}
          data-testid="board-filter-name"
        />
        <Select
          label="Zone"
          placeholder="any zone"
          data={zoneOptions}
          value={filters.zone}
          onChange={setFilter("zone")}
          searchable
          clearable
          size="xs"
          w={200}
          data-testid="board-filter-zone"
        />
        {filtered ? (
          <Button
            variant="subtle"
            size="compact-xs"
            onClick={clearFilters}
            data-testid="board-filter-clear"
          >
            clear
          </Button>
        ) : null}
        {/* The denominator, so a narrow result is a measurement rather than
            a board that looks broken. `0 of 12` is a statement; an empty
            table under an unremarked filter is not. */}
        {filtered ? (
          <span className="ddns-note" data-testid="board-filter-count">
            showing {rows.length} of {allRows.length}
          </span>
        ) : null}
      </Group>
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
        {rows.length === 0 ? (
          <span className="ddns-note" data-testid="board-no-match">
            No row matches that filter. {allRows.length} in total — clear the
            filter to see them.
          </span>
        ) : null}
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
              {/* The device cell. Two things, pushed apart: the device
                  name on the left, which opens the card, and the
                  add-a-name `+` on the right.

                  It carried a trash icon until #155 — a one-click delete
                  of the *device*, and of every name assigned to it, on a
                  row that is about a *hostname*. Deleting is still
                  reachable, from the card the name opens, which asks
                  first and says how many names go with the device.

                  The `+` moved in here from the name cell in #154, and
                  the reason is the column, not the tidiness. The name
                  column is `minmax(12rem, 1fr)` and
                  `.ddns-boardtable__row > *` is `overflow: hidden` with
                  `white-space: nowrap`, so on a long name the cell ran
                  out of room and the thing pushed past the edge was the
                  **control** rather than the text — the ellipsis landed
                  on the affordance. It vanished silently and it vanished
                  precisely on the longest names. The device column is
                  `max-content`: it is sized to what it holds, so there
                  is no room for it to run out of.

                  It also reads correctly here, which the placement
                  beside the name never did. "Add a name" is an action on
                  the *device* — `boardNameNewHref(row.device?.id)`
                  presets it, and has since #128 — so beside the hostname
                  it looked like an action on that hostname, which it
                  never was.

                  `justify="space-between"` is what right-aligns it. In a
                  `max-content` track the group is exactly as wide as the
                  widest row's contents, so the `+` sits on the column's
                  right edge and the shorter device names do not drag it
                  left with them. */}
              <Group gap={4} wrap="nowrap" align="center" justify="space-between">
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
                {/* Only on rows that *have* a name. The "no names yet"
                    row has its own add control in the cell below, which
                    is the row where it matters most and where it reads
                    as the answer to the text beside it; a second one
                    here would be the duplicate this issue removed.

                    A row with no device still gets one. It presets
                    nothing — `boardNameNewHref(undefined)` omits `for=`
                    — but the whole argument for this table is that the
                    columns stay where they were, and a `+` that is in
                    the device column on most rows and missing on some
                    is the shape-changing layout the flat table replaced. */}
                {row.hostname ? (
                  <Tooltip
                    label={
                      row.device
                        ? `Add a name for ${row.device.name}`
                        : "Add a name"
                    }
                    withArrow
                  >
                    <ActionIcon
                      component="a"
                      href={boardNameNewHref(row.device?.id)}
                      variant="subtle"
                      color="gray"
                      size="sm"
                      aria-label={
                        row.device
                          ? `Add a name for ${row.device.name}`
                          : "Add a name"
                      }
                      data-testid={`board-add-name-${row.hostname.name}`}
                    >
                      <IconPlus size={15} />
                    </ActionIcon>
                  </Tooltip>
                ) : null}
              </Group>
              {row.hostname ? (
                /* The name, and only the name. It is the direct grid
                   child now rather than the first item of a group, which
                   is what puts `.ddns-boardtable__row > *`'s
                   `text-overflow: ellipsis` on the anchor itself — so a
                   name too long for the column truncates with an
                   ellipsis, which is what that rule was written to do.
                   With a wrapper in between, the rule applied to the
                   wrapper and the anchor overflowed it intact. */
                <a className="ddns-data" href={boardNameHref(row.hostname.id)}>
                  {row.hostname.name}
                </a>
              ) : (
                <Group gap={4} wrap="nowrap" align="center">
                  <span className="ddns-cell" data-tone="quiet">
                    no names yet
                  </span>
                  {/* The row the `+` matters most on. A device with no names is the
                      state the board's own empty text tells you to fix — "register a
                      name for it to update" — and it was the one row with no way to
                      act on it. The device is preselected because this row knows
                      which one it is. */}
                  <Tooltip label="Add a name for this device" withArrow>
                    <ActionIcon
                      component="a"
                      href={boardNameNewHref(row.device?.id)}
                      variant="subtle"
                      color="gray"
                      size="sm"
                      aria-label={`Add a name for ${row.device?.name ?? "this device"}`}
                      data-testid={`board-add-name-for-${row.device?.name}`}
                    >
                      <IconPlus size={15} />
                    </ActionIcon>
                  </Tooltip>
                </Group>
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
                <Tooltip label="Show this name in the log" withArrow>
                  <LogLink
                    params={{ hostname_id: row.hostname.id }}
                    aria-label={`Log for ${row.hostname.name}`}
                    data-testid={`board-log-${row.hostname.name}`}
                  >
                    <IconListSearch size={15} />
                  </LogLink>
                </Tooltip>
              ) : (
                <span />
              )}
            </div>
          );
        })}
      </div>
      {/* No delete-confirmation modal here. It went with the trash icon in
          #155, because it was the only thing that opened it — a modal
          nothing can open is the same artefact as a metric nothing writes.
          The confirmation the operator sees is `DeviceCard`'s own, which
          is reached by clicking the device name, and it says the same
          sentence from a surface that is actually about the device. */}
    </>
  );
}
