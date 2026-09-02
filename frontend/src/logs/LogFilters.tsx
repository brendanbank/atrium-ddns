/** The filter bar, and the chips that say what is applied.
 *
 * Two halves, and they are two on purpose.
 *
 * **The controls** are body face — Mantine `Select`s and `TextInput`s,
 * inheriting the operator's branding. `docs/ops/ui-design.md` §2.4
 * confines the `;` zone-file-comment convention to machine-data column
 * heads and station labels: *"not on form fields, not on section
 * titles, not on empty states. One borrowed convention used twice is a
 * motif; used everywhere it is a costume."* A form field says what it
 * is out loud, so §2.2's boundary rule puts it in the body face.
 *
 * **The chips** render `page.filters` — the filters the **server ran
 * with**, not the filters this component holds. That distinction is the
 * point: an empty result is only interpretable beside the filters that
 * produced it, and a UI that renders its own filter state next to a
 * server's rows is two sources of truth for one question. If a filter
 * fails to reach the query, the chip disappears and the discrepancy is
 * visible instead of silent.
 *
 * **Nothing here is accented.** §1.2 Rule 2: `--ddns-diverge` means
 * *this is true and it is wrong*, never *you can do this*. Every
 * control on this bar belongs to `--mantine-primary-color-*`, which the
 * operator owns.
 *
 * ## Why the selects are populated from the payload
 *
 * `vocabulary` is derived on the server from the writer's own constants
 * and the provider registry. A hardcoded list here would offer options
 * this installation cannot produce — a dropdown entry that matches
 * nothing, which to a user is indistinguishable from "that has not
 * happened lately". `checkip` and `healthcheck` are the live example:
 * both appear in `models.DnsEvent`'s own comment and neither has a
 * writer, so neither is offered.
 */
import {
  Button,
  Group,
  Pill,
  Select,
  Stack,
  TextInput,
} from '@mantine/core';

import {
  QUERY_KEYS,
  activeFilterCount,
  type EventFilters,
  type EventVocabulary,
  type LogQuery,
} from '../api/events';
import { filterLabel } from './format';

export interface LogFiltersProps {
  query: LogQuery;
  vocabulary: EventVocabulary;
  /** What the server actually ran with. */
  applied: EventFilters;
  /** True when this caller reads across tenants. The user filter is
   *  shown only then — and the decision arrives from the server rather
   *  than being re-derived from a permission list in the browser. */
  crossTenant: boolean;
  /** The things the id filters are *about*.
   *
   *  `device_id`, `hostname_id` and `domain_id` were filterable and
   *  labelled but had no control at all — reachable only by following a
   *  link that already knew the id. So the log could be filtered to a name
   *  and never *by* one, and the chip it produced read `hostname: 1`,
   *  which is the id rendered as if it were the answer.
   *
   *  One list feeds both the select and the chip, so a filter and its own
   *  description cannot disagree about what id 1 is called. */
  devices: { id: number; name: string }[];
  hostnames: { id: number; name: string }[];
  domains: { id: number; name: string }[];
  /** The users seen in the rows currently loaded, `{id, email}`.
   *
   *  Derived from the result rather than fetched: this bundle owns no
   *  user directory — atrium does — and the rows already carry
   *  `user_email` beside `user_id`, which is what the ledger renders. It
   *  is enough for the two jobs here. Resolving the *chip* is exact: a
   *  result filtered to one user contains that user on every row. The
   *  dropdown is necessarily partial — it can only offer users who appear
   *  in what is loaded — and that is stated in its placeholder rather
   *  than left to be discovered. */
  users: { id: number; email: string }[];
  onChange: (key: keyof LogQuery, value: string) => void;
  onClear: () => void;
}

/** `null` -> `''`, because a Mantine `Select` clears to `null` and the
 *  query string's "not filtered" is the empty string. Folding the two
 *  at the boundary keeps every other reader from having to know. */
function cleared(value: string | null): string {
  return value ?? '';
}

export function LogFilters({
  query,
  vocabulary,
  devices,
  hostnames,
  domains,
  users,
  applied,
  crossTenant,
  onChange,
  onClear,
}: LogFiltersProps) {
  const count = activeFilterCount(query);

  // Driven off the server's echo, keyed by the same `QUERY_KEYS` the URL
  // reader and writer walk. A filter added to `EMPTY_QUERY` appears here
  // with nothing edited.
  /** The name behind an id filter, or the id when nothing matches it.
   *
   *  Falling back to the id rather than to `—` or a blank: an id the
   *  current lists do not contain is a real state — a deleted device, a
   *  cross-tenant read, a link pasted from someone else's account — and
   *  `hostname: 1` is at least true, where `hostname: —` would claim the
   *  filter is empty when the rows below are filtered by it. */
  /** Whatever arrived, as something safe to `.map`.
   *
   *  `?? []` was not enough and the difference took the whole page down.
   *  It guards `null` and `undefined`; a lookup that answers `{}` — an
   *  error body, an endpoint that moved, a stub that returns a bare
   *  object — is neither, so `rows.map` threw during render and, with no
   *  error boundary over the host root, React unmounted everything. The
   *  log rendered nothing because a *filter option list* was the wrong
   *  shape. Refuse the shape here instead: the selects go empty and the
   *  log still reads, which is what the fallback was always meant to do. */
  const list = (rows: unknown): { id: number; name: string }[] =>
    Array.isArray(rows) ? (rows as { id: number; name: string }[]) : [];

  const nameFor = (key: string, id: string): string => {
    const rows =
      key === 'device_id'
        ? list(devices)
        : key === 'hostname_id'
          ? list(hostnames)
          : key === 'domain_id'
            ? list(domains)
            : key === 'user_id'
              ? list(users.map((u) => ({ id: u.id, name: u.email })))
              : null;
    if (rows === null) return id;
    return rows.find((row) => String(row.id) === id)?.name ?? id;
  };

  const chips = QUERY_KEYS.map((key) => {
    const value = (applied as unknown as Record<string, unknown>)[key];
    if (value === null || value === undefined || value === '') return null;
    return { key, value: nameFor(key, String(value)) };
  }).filter((chip): chip is { key: keyof LogQuery; value: string } =>
    chip !== null,
  );

  /** Mantine wants `{value,label}` with a string value; ids are numbers. */
  const options = (rows: unknown) =>
    list(rows).map((row) => ({ value: String(row.id), label: row.name }));

  return (
    <Stack gap="xs" data-testid="log-filters">
      <Group gap="sm" align="flex-end" wrap="wrap">
        {crossTenant ? (
          <Select
            label="User"
            placeholder="anyone in these rows"
            description="Everyone's rows are visible to you"
            data={options(users.map((u) => ({ id: u.id, name: u.email })))}
            value={query.user_id || null}
            onChange={(value) => onChange('user_id', cleared(value))}
            searchable
            clearable
            data-testid="filter-user-id"
            w={240}
          />
        ) : null}
        <Select
          label="Event"
          placeholder="any"
          clearable
          data={vocabulary.event_types}
          value={query.event_type || null}
          onChange={(value) => onChange('event_type', cleared(value))}
          data-testid="filter-event-type"
          w={140}
        />
        <Select
          label="Result"
          placeholder="any"
          clearable
          data={vocabulary.response_codes}
          value={query.response_code || null}
          onChange={(value) => onChange('response_code', cleared(value))}
          data-testid="filter-response-code"
          w={140}
        />
        <Select
          label="Backend"
          placeholder="any"
          clearable
          /* The third state gets an option of its own. `NULL` here is a
             meaning — "decided before any backend was contacted" — and
             a control that can only express *any* and *this provider*
             cannot ask the third question at all. The sentinel's
             spelling comes from the payload so there is one of it. */
          data={[
            ...vocabulary.backend_types.map((value) => ({
              value,
              label: value,
            })),
            { value: vocabulary.backend_type_none, label: 'no backend reached' },
          ]}
          value={query.backend_type || null}
          onChange={(value) => onChange('backend_type', cleared(value))}
          data-testid="filter-backend-type"
          w={170}
        />
        {/* The three id filters, as things you can pick by name.
            `searchable` because a tenant with forty names should type, not
            scroll; `clearable` because removing a filter has to be as easy
            as adding one, and the chip's remove button is the other way.
            They sit before `Called from` so the two questions a log is
            usually asked — *which device* and *which name* — come first. */}
        <Select
          label="Device"
          placeholder="any device"
          data={options(devices)}
          value={query.device_id || null}
          onChange={(value) => onChange('device_id', cleared(value))}
          searchable
          clearable
          data-testid="filter-device-id"
          w={190}
        />
        <Select
          label="Hostname"
          placeholder="any name"
          data={options(hostnames)}
          value={query.hostname_id || null}
          onChange={(value) => onChange('hostname_id', cleared(value))}
          searchable
          clearable
          data-testid="filter-hostname-id"
          w={220}
        />
        <Select
          label="Zone"
          placeholder="any zone"
          data={options(domains)}
          value={query.domain_id || null}
          onChange={(value) => onChange('domain_id', cleared(value))}
          searchable
          clearable
          data-testid="filter-domain-id"
          w={200}
        />
        <TextInput
          label="Called from"
          placeholder="address"
          value={query.client_ip}
          onChange={(event) => onChange('client_ip', event.currentTarget.value)}
          data-testid="filter-client-ip"
          /* §2.5's budget: 380px for one address cell. The widest real
             IPv6 in this estate is 39 characters and never
             `::`-compressed (M2), so a 200px input truncates what the
             user just pasted out of a log line. */
          w={220}
        />
        <TextInput
          label="Since"
          placeholder="2026-08-01T00:00:00Z"
          value={query.since}
          onChange={(event) => onChange('since', event.currentTarget.value)}
          data-testid="filter-since"
          w={220}
        />
        <TextInput
          label="Until"
          placeholder="2026-08-15T00:00:00Z"
          value={query.until}
          onChange={(event) => onChange('until', event.currentTarget.value)}
          data-testid="filter-until"
          w={220}
        />
      </Group>

      <Group gap="xs" align="center" data-testid="log-applied">
        {chips.length === 0 ? (
          /* A measured zero, not a silence. "No filters — showing
             everything" is a statement about what the rows below are;
             an empty row of chips is indistinguishable from a chip strip
             that failed to render. */
          <span className="ddns-label" data-testid="log-applied-none">
            no filters — showing everything in the window
          </span>
        ) : (
          chips.map((chip) => (
            <Pill
              key={chip.key}
              withRemoveButton
              onRemove={() => onChange(chip.key, '')}
              data-testid={`log-applied-${chip.key}`}
            >
              {filterLabel(chip.key)}: {chip.value}
            </Pill>
          ))
        )}
        {count > 0 ? (
          <Button
            variant="subtle"
            size="compact-xs"
            onClick={onClear}
            data-testid="log-clear-filters-bar"
          >
            clear all
          </Button>
        ) : null}
      </Group>
    </Stack>
  );
}
