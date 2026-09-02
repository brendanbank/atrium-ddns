/** The log, as a ledger of two-line entries.
 *
 * ## The layout decision, and the arrangement it kills
 *
 * `docs/ops/ui-design.md` §3.1 rejected a three-column strip by
 * arithmetic rather than by taste, and the same arithmetic decides this
 * surface. §2.5 budgets **380px for one address cell** — the widest real
 * IPv6 in this estate is 39 characters and never `::`-compressed (M2),
 * and the column ceiling is 45.
 *
 * **Arrangement A — one table row per event, addresses as columns.**
 * The obvious shape. A log line carries *two* addresses (`client_ip`,
 * the address it called from, and `ip`, the address it was about), so
 * the minimum is 2 × 380 + a time column + a device + a hostname + a
 * result + a backend ≈ 760 + 90 + 140 + 260 + 80 + 90 = **1420px**.
 * Atrium's AppShell leaves 1168px at a 1440px viewport and 1008px on a
 * 1280px laptop (§3.1's own measurement). It does not fit, and the
 * failure mode is the one §3.1 disqualified an entire arrangement for:
 * a wrapped IPv6 address breaks at an arbitrary character with no
 * visual mark, so **the overflow behaviour is "silently show a
 * different address"**.
 *
 * **Arrangement B — a fielded head line, addresses on their own line
 * beneath.** Chosen. The head line carries the short fields (when,
 * device, name, result, backend, and the user when cross-tenant); the
 * address line spans the full width, so one address has 380px with room
 * to spare and two have 760px inside 1168px. Minimum ≈ 780px, which
 * fits one-up at tablet width.
 *
 * M3 pays for it twice over: 94.5% of real updates are `nochg`, where
 * the device sent no `myip` and the two addresses are the same fact. So
 * **the second address is rendered only when it differs** — and when it
 * does, that difference is the interesting part of a NAT'd update
 * (`router_nic.py:670`), which this layout gives room to say in words
 * rather than hiding in a truncated column.
 *
 * ## Colour
 *
 * §1.2 Rule 1 — agreement has no colour. A `good` or `nochg` line is
 * plain ink on the page background; nothing about it is tinted, badged
 * or filled. Rule 2 — `--ddns-diverge` appears **only** on a measured
 * disagreement, which on this surface is a non-success response code,
 * classified by the server's own `success_response_codes`. Rule 3 — it
 * never travels alone: the accented code carries the `≠` glyph and a
 * screen-reader word, because in the dark scheme the accent and the ink
 * differ in luminance by 1.42:1 and a greyscale render cannot tell them
 * apart.
 *
 * A **deleted** device is not accented. §4.1 already ruled on the
 * shape: an unassigned hostname "is a configuration state, not a fault,
 * and must not be marked". A device someone deleted is the same kind of
 * fact.
 */
import type { EventPage, EventRow, LogQuery } from '../api/events';
import { absoluteTitle, formatStationTime } from '../board/format';
import {
  backendCell,
  nameCell,
  responseGlyph,
  responseTone,
  responseWord,
} from './format';

export interface LogLedgerProps {
  page: EventPage;
  onFilter: (key: keyof LogQuery, value: string) => void;
  onOpen: (row: EventRow) => void;
}

/** A denormalised name, rendered — and made a filter only when there is
 *  something left to filter on.
 *
 * The three outcomes are three renderings. A deleted device keeps its
 * name (that is what the denormalised column is for) and loses its
 * link, with the word `deleted` saying why rather than leaving the
 * reader to notice that one row is not clickable. An inert link would
 * be worse than no link: it filters on nothing and returns an empty log
 * that reads as "this device did nothing". */
function NameCell({
  name,
  id,
  filterKey,
  onFilter,
  title,
  testid,
}: {
  name: string | null;
  id: number | null;
  /** The full value, for when the cell is truncated. Truncation without
   *  it loses information; with it, it defers information. */
  title?: string;
  filterKey: keyof LogQuery;
  onFilter: (key: keyof LogQuery, value: string) => void;
  testid: string;
}) {
  const cell = nameCell(name, id);
  if (!cell.filterable) {
    return (
      <span
        className="ddns-data"
        data-tone={cell.tone}
        title={title}
        data-testid={testid}
      >
        {cell.text}
        {cell.deleted ? (
          <span className="ddns-log__gone" data-testid={`${testid}-deleted`}>
            {' '}
            (deleted)
          </span>
        ) : null}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="ddns-log__filter"
      onClick={() => onFilter(filterKey, String(id))}
      title={`show only ${cell.text}`}
      data-testid={testid}
    >
      {cell.text}
    </button>
  );
}

function LogEntry({
  row,
  index,
  page,
  onFilter,
  onOpen,
}: {
  row: EventRow;
  /** Stripe parity, stated rather than counted — the head is a sibling
   *  in the same grid. */
  index: number;
  page: EventPage;
  onFilter: (key: keyof LogQuery, value: string) => void;
  /** Opens the full detail. The ledger does not own the modal; the page
   *  does, so one place decides what "open an event" means. */
  onOpen: (row: EventRow) => void;
}) {
  const tone = responseTone(
    row.response_code,
    page.vocabulary.success_response_codes,
  );
  const glyph = responseGlyph(tone);
  const backend = backendCell(row.backend_type);
  // The NAT'd case. Rendered only when the two facts differ — which M3
  // makes the uncommon case — because a second address that always
  // repeats the first is a column that has stopped carrying anything.
  const declared =
    row.ip !== null && row.ip !== row.client_ip ? row.ip : null;

  return (
    <div
      className="ddns-logs__row"
      data-stripe={index % 2 === 1 ? 'on' : 'off'}
      data-testid={`log-row-${row.id}`}
      data-tone={tone}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(row)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen(row);
        }
      }}
    >
      <span
        className="ddns-cell"
        title={absoluteTitle(row.created_at)}
        data-testid={`log-row-${row.id}-when`}
      >
        {formatStationTime(row.created_at)}
      </span>
      <NameCell
        name={row.device_name}
        id={row.device_id}
        filterKey="device_id"
        onFilter={onFilter}
        testid={`log-row-${row.id}-device`}
      />
      <NameCell
        name={row.hostname}
        id={row.hostname_id}
        filterKey="hostname_id"
        onFilter={onFilter}
        testid={`log-row-${row.id}-hostname`}
      />
      <span className="ddns-cell" data-testid={`log-row-${row.id}-event`}>
        {row.event_type}
      </span>
      <span
        className="ddns-cell ddns-log__result"
        data-tone={tone}
        data-testid={`log-row-${row.id}-result`}
      >
        {glyph ? (
          <span className="ddns-log__glyph" aria-hidden="true">
            {glyph}{' '}
          </span>
        ) : null}
        {row.response_code ?? 'no answer'}
        <span className="ddns-sr"> — {responseWord(tone)}</span>
      </span>
      <span
        className="ddns-cell"
        data-tone={backend.tone}
        title={backend.title}
        data-testid={`log-row-${row.id}-backend`}
      >
        {backend.text}
      </span>
      {/* `called from` was a second line under every row. It is a
          short value and it belongs in the table; the rest of the
          detail — declared myip, the message, the ids — moved into the
          modal a click opens, because it mattered on the few rows you
          stop at and every row was paying for it. */}
      <span className="ddns-cell" data-testid={`log-row-${row.id}-client-ip`}>
        {row.client_ip ?? 'not recorded'}
        {declared !== null ? (
          <span className="ddns-log__declared" title={`declared myip ${declared}`}>
            {' '}≠
          </span>
        ) : null}
      </span>
      {page.cross_tenant ? (
        <NameCell
          name={row.user_email}
          id={row.user_id}
          filterKey="user_id"
          onFilter={onFilter}
          testid={`log-row-${row.id}-user`}
        />
      ) : null}
    </div>
  );
}

export function LogLedger({ page, onFilter, onOpen }: LogLedgerProps) {
  return (
    <div className="ddns-logs" data-testid="log-ledger">
      {/* §2.4's borrowed convention, on machine-data column heads —
          the same kind of object as the device board's heads, and the
          same rendering. Not on the filter bar's form labels. */}
      {/* Sentence-case headings on the shared `.ddns-th`, matching the
          zone, device and name tables. `.ddns-label` gave these the data
          face and a `; ` marker; that borrowing belongs to the strip's
          station labels, not to a column head. */}
      <div className="ddns-logs__head" data-testid="log-head">
        <span className="ddns-th">When</span>
        <span className="ddns-th">Device</span>
        <span className="ddns-th">Hostname</span>
        <span className="ddns-th">Event</span>
        <span className="ddns-th">Result</span>
        <span className="ddns-th">Via</span>
        <span className="ddns-th">Called from</span>
        {page.cross_tenant ? <span className="ddns-th">User</span> : null}
      </div>
      {page.rows.map((row, index) => (
        <LogEntry
          key={row.id}
          row={row}
          index={index}
          page={page}
          onFilter={onFilter}
          onOpen={onOpen}
        />
      ))}
    </div>
  );
}

/** §4.5's loading state, at ledger scale. Static grey blocks, no
 *  shimmer — §3.7 allows exactly one transition in this design and it
 *  is on the strip's rail, not here. */
export function LogSkeleton() {
  return (
    <div className="ddns-log" data-testid="log-loading">
      <div className="ddns-log__skeleton" />
      <div className="ddns-log__skeleton" />
      <div className="ddns-log__skeleton" />
    </div>
  );
}
