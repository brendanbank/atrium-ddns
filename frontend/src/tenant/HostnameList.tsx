/** Names — create, assign, reassign, delete.
 *
 * The surface that closes the loop. #44 built the board, #45 built zones
 * and devices, #46 built the log, and none of them could create the
 * object the other three describe.
 *
 * ## Three states for the device column, not two
 *
 * `Not assigned yet` is a real configuration state and is rendered as a
 * choice rather than as a blank: the model allows a name to exist before
 * anything is assigned to it, and `ON DELETE SET NULL` means a name
 * outlives the router that was updating it. A blank cell would read as
 * missing data, which is the `n/a` is never `0` failure wearing a
 * select's clothing.
 *
 * ## No validation here
 *
 * There is no regex in this file and there must never be one. The server
 * decides validity with the same two functions `/nic/update` uses, and
 * its refusal is rendered verbatim — see `api/hostnames.ts` for why a
 * third opinion would be worse than a round trip.
 *
 * What the form *does* do is show the exact string it is about to send,
 * because it composes and trims and the API does neither. `will send:`
 * is not decoration: without it, a pasted trailing space produces a
 * refusal about a value that does not look like what is on screen.
 *
 * ## The zone is a suffix, not a retype (#90, design §13)
 *
 * The form used to send `name.trim()` verbatim, so the zone chosen in
 * the select above contributed `domain_id` and nothing else — the
 * operator picked `example.invalid` and then typed it again. Now the
 * zone is rendered as a fixed suffix inside the field and
 * `composeHostname` joins the two.
 *
 * The line between *composing* and *validating* is the whole point of
 * the change, and it is drawn at: **the composer never blocks and never
 * decides.** It produces one string, that string is what `will send:`
 * shows, and that string is what leaves the browser. Whether it is a
 * legal name is answered by the server, once, in its own words.
 */
import {
  Anchor,
  Button,
  Group,
  Stack,
  Text,
} from '@mantine/core';

import type { Device } from '../api/devices';
import type { Domain } from '../api/domains';
import {
  type Hostname,
} from '../api/hostnames';
import { absoluteTitle, formatAge } from '../board/format';
import { opensInThisTab } from '../cards';
import { namesHrefForName } from '../paths';
import { NAMES_PATH } from '../HostnamesPage';

/** The `value` a Mantine `Select` uses for *no device*. `Select` speaks
 *  `string | null`, and `null` is already how it spells "nothing
 *  chosen" — which is a different fact from "chosen: unassigned". Two
 *  facts, two values. */

/** Join what was typed to the zone that was selected. Two rules, and
 *  deliberately no third.
 *
 *  1. Trim. The API does not, and a pasted trailing space is otherwise
 *     a refusal about a value that does not look like what is on screen.
 *  2. Append `.<zone>` **unless what was typed already ends with the
 *     zone** — the paste tolerance §13 requires, because operators paste
 *     FQDNs out of zone files and tickets and
 *     `home.example.invalid.example.invalid` is not what they meant.
 *
 *  ### Why this is not a second `zone_contains`
 *
 *  The suffix test here and `providers/base.py`'s `zone_contains` are
 *  the same string primitive, and saying otherwise would be a dodge:
 *  `rfind` plus an end-offset check *is* `endsWith`. What makes this not
 *  the second implementation the backend was warned about is that the
 *  two answer different questions and only one of them is believed.
 *  `zone_contains` decides **whether the row may exist**; this decides
 *  **whether to type four more characters for you**. Nothing branches
 *  on the answer, nothing is blocked, no request is withheld. Get it
 *  wrong and the operator sees a wrong string in `will send:` and the
 *  server refuses it — which is the same outcome as typing it wrong by
 *  hand, and is why there is no client-side pre-check to drift.
 *
 *  ### The trailing dot is deliberately not special-cased
 *
 *  `home.example.invalid.` does not end with `example.invalid`, so the
 *  suffix is appended and `will send:` reads
 *  `home.example.invalid..example.invalid`. That looks like a bug and is
 *  a decision. To do better this function would have to know that a
 *  trailing dot marks the root — a fact about the label rule, which is
 *  exactly the knowledge §13.1 says must not live here. It is not even
 *  a harmless fact: `zone_contains('example.com', 'foo.example.com.')`
 *  is **False**, so a browser that quietly dropped the dot would be
 *  accepting a byte sequence the server refuses, which is the `.strip()`
 *  incident rewritten in TypeScript. The preview shows the absurd
 *  string before anything is sent; one keystroke fixes it.
 *
 *  Exported so the table in `HostnamesPage.test.tsx` can drive it
 *  directly. It has exactly one caller.
 */
/** The sentinel the create form is reached by. `NameModal` reads a
 *  `null` id as create; this list only knows "open something", so it
 *  hands the page a value the page maps. */
export const NEW_NAME = -1;

export function composeHostname(typed: string, zone: string | null): string {
  const entered = typed.trim();
  // Nothing typed is nothing to send — not the zone apex. An empty
  // field is an absence of input, and inventing `example.invalid` from
  // it would submit a name the operator never wrote.
  if (entered === '') return '';
  if (zone === null || zone === '') return entered;
  if (entered.toLowerCase().endsWith(zone.toLowerCase())) return entered;
  return `${entered}.${zone}`;
}

/** The inverse: the label a name carries under its zone.
 *
 * `NameModal` seeds its Name box from a stored row, and the box holds
 * the label rather than the FQDN — so it has to answer the same question
 * `composeHostname` does, backwards: *does this name already end with
 * its zone?*
 *
 * It lives here, beside the composer, because that question having two
 * homes is exactly the drift `HostnamesPage.test.tsx` sweeps for — and
 * the sweep found it, in `NameModal` with a `.${zone}` suffix while the
 * composer matched on the bare zone. Two spellings of one rule disagree
 * on the apex: `example.net` under zone `example.net` is the zone
 * itself, which the composer leaves alone and the modal's copy did not.
 *
 * Round-trips with `composeHostname`: `compose(decompose(n, z), z) === n`
 * for any `n` that ends with `z`.
 */
export function decomposeHostname(name: string, zone: string | null): string {
  if (zone === null || zone === '') return name;
  if (!name.toLowerCase().endsWith(zone.toLowerCase())) return name;
  if (name.length === zone.length) return name;
  // Strip the separating dot too, but only if that is what is there —
  // `notexample.net` ends with `example.net` and is not under it.
  const label = name.slice(0, name.length - zone.length);
  return label.endsWith('.') ? label.slice(0, -1) : name;
}


function HostnameLine({
  hostname,
  index,
  deviceName,
  onOpen,
}: {
  hostname: Hostname;
  /** Stripe parity, stated rather than counted — the head is a sibling
   *  in the same grid, and the selector that would count correctly is
   *  rejected by lightningcss. */
  index: number;
  /** Resolved by the caller, which already holds the device list. The
   *  row does not fetch and does not mutate; it renders. */
  deviceName: string | null;
  onOpen: (id: number) => void;
}) {
  return (
    <div
      className="ddns-names__row"
      data-stripe={index % 2 === 1 ? 'on' : 'off'}
      data-testid={`hostname-${hostname.name}`}
    >
      {/* The name opens the one modal that holds every setting for it.
          The row used to carry a device dropdown and a gear: the list
          could mutate data while looking like a list, and the settings
          for one object lived in three places. */}
      <a
        className="ddns-data"
        href={namesHrefForName(hostname.id)}
        onClick={(event) => {
          if (!opensInThisTab(event.nativeEvent)) return;
          event.preventDefault();
          onOpen(hostname.id);
        }}
        data-testid={`hostname-${hostname.name}-link`}
      >
        {hostname.name}
      </a>
      <span className="ddns-cell">{hostname.domain_name}</span>
      <span className="ddns-cell" data-testid={`assigned-${hostname.name}`}>
        {deviceName ?? 'Not assigned'}
      </span>
      <span
        className="ddns-cell"
        title={absoluteTitle(hostname.last_updated_at)}
      >
        {/* `never` for a null, never an epoch-derived age. */}
        {formatAge(hostname.last_updated_at)}
      </span>
    </div>
  );
}

export function HostnameList({
  hostnames,
  domains,
  devices,
  zoneFilter = null,
  nameFilter = null,
  onOpen,
}: {
  hostnames: Hostname[];
  domains: Domain[];
  devices: Device[];
  /** Set by `?zone=` — the zone list's "N names" link. */
  zoneFilter?: number | null;
  /** Set by `?name=` — the device card's link to one name. */
  nameFilter?: number | null;
  /** Opens the one modal that holds every setting for a name. The list
   *  does not own it: `HostnamesPage` does, so the address bar decides
   *  what is open and a reload restores it. */
  onOpen: (id: number) => void;
}) {
  /** Filtering is a view, so it is applied here rather than by refetching
   *  a narrower query: the list is already loaded, and a second query
   *  keyed by filter would give this page two caches of the same rows
   *  that could disagree after a mutation. */
  const shown = hostnames.filter(
    (h) =>
      (zoneFilter === null || h.domain_id === zoneFilter) &&
      (nameFilter === null || h.id === nameFilter),
  );


  return (
    <Stack gap="md">


      {domains.length === 0 ? (
        <Text size="sm" data-testid="hostnames-no-zone">
          {/* The next action, not a dead end. A name has to live in a
              zone, so there is nothing useful to offer until one
              exists. */}
          You have no zones yet, and a name has to live in one. Add a zone
          first, then come back and register a name under it.
        </Text>
      ) : hostnames.length === 0 ? (
        <Text size="sm" data-testid="hostnames-empty">
          You have no names yet. Register one, then point a device at it — the
          board draws a resolution strip once that device has published an
          address.
        </Text>
      ) : shown.length === 0 ? (
        /* Filtered to nothing is not the same as owning nothing.
           Telling someone with twelve names that they have none is
           a claim about their account that is untrue. */
        <Text size="sm" data-testid="hostnames-no-match">
          No name matches that filter. {hostnames.length} name
          {hostnames.length === 1 ? '' : 's'} in total —{' '}
          <Anchor href={NAMES_PATH} data-testid="hostnames-clear-filter">
            show all
          </Anchor>
          .
        </Text>
      ) : (
        <div className="ddns-names" data-testid="names-table">
          {/* Sentence-case headings on the shared `.ddns-th`, and no
              `; ` marker — the same table treatment as zones and
              devices, sharing their classes rather than copying the
              declarations. */}
          <div className="ddns-names__head">
            <span className="ddns-th">Name</span>
            <span className="ddns-th">Zone</span>
            <span className="ddns-th">Device</span>
            <span className="ddns-th">Last published</span>
            <span className="ddns-th" />
            <span className="ddns-th" />
          </div>
          {shown.map((hostname, index) => (
            <HostnameLine
              key={hostname.id}
              hostname={hostname}
              index={index}
              deviceName={
                devices.find((d) => d.id === hostname.device_id)?.name ?? null
              }
              onOpen={onOpen}
            />
          ))}
        </div>
      )}

      <Group>
        <Button
          size="xs"
          disabled={domains.length === 0}
          onClick={() => onOpen(NEW_NAME)}
          data-testid="add-hostname"
        >
          Register a name
        </Button>
      </Group>
    </Stack>
  );
}
