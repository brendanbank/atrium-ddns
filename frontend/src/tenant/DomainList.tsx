/** The zone list — a table, and nothing else.
 *
 * It used to own the create modal, the create mutation, the provider
 * catalogue and the open-zone state. All four moved: the modal is one
 * component (`ZoneModal`) reached from the URL, so this file renders
 * rows and links and holds no state at all.
 *
 * ## Why the row grew columns
 *
 * It carried the zone name and two counts — "3 names · 1 provider" —
 * which restated what the card would show and told you nothing you could
 * act on. The operator asked for the provider and a way through to the
 * names, so the row is now three fields:
 *
 *     zone            provider        names
 *
 * `provider` is the thing you most often want to check without opening
 * anything ("which of these is on Route 53?"), and `names` is a link to
 * the surface that owns them — the card deliberately no longer lists
 * names, because that is a different interface.
 *
 * ## Why a zone with no provider still renders diverged
 *
 * The create flow requires one, so this state is only reachable by
 * legacy import. It is still the row you must not scroll past: every
 * update for a name under it answers `911`, frozen at
 * `tests/compat/protocol_cases.yaml:211`. `ZoneStatus` owns the words.
 */
import { Anchor, Text } from '@mantine/core';

import { type Domain } from '../api/domains';
import { opensInThisTab } from '../cards';
import { namesHrefForZone, zoneHrefParam } from '../paths';
import { ZoneNowhereMark, ZoneWireConsequence } from './ZoneStatus';

function ZoneRow({
  domain,
  index,
  onOpen,
}: {
  domain: Domain;
  /** Row position, only so the row can state its own stripe parity.
   *  CSS cannot count these reliably: the head is a sibling in the same
   *  grid so `:nth-child(even)` is off by one, and `:nth-child(even of
   *  .ddns-zones__row)` — which would be right — is rejected by
   *  lightningcss at build time. Compensating with `odd` would work
   *  until the head moved, and then fail silently. */
  index: number;
  onOpen: (id: number) => void;
}) {
  const nowhere = domain.backends.length === 0;
  const provider = domain.backends[0]?.backend_type ?? null;
  return (
    <div
      className="ddns-zones__row"
      data-stripe={index % 2 === 1 ? 'on' : 'off'}
      data-diverged={nowhere ? 'true' : 'false'}
      data-testid={`domain-${domain.name}`}
    >
        {/* A real `href`, and a plain click is intercepted only to keep
            the SPA from reloading. cmd/ctrl/shift/middle click still
            navigates, so "open in a new tab" and "copy link address"
            behave exactly as the anchor promises — and the address it
            copies opens the modal, because the modal is the URL. */}
        <Anchor
          href={zoneHrefParam(domain.id)}
          className="ddns-data"
          onClick={(event) => {
            if (!opensInThisTab(event.nativeEvent)) return;
            event.preventDefault();
            onOpen(domain.id);
          }}
          data-testid={`open-domain-${domain.name}`}
        >
          {domain.name}
        </Anchor>
        <span className="ddns-cell" data-testid={`provider-${domain.name}`}>
          {provider ?? '—'}
        </span>
        <Anchor
          href={namesHrefForZone(domain.name)}
          size="sm"
          data-testid={`names-${domain.name}`}
        >
          {domain.hostname_count} name{domain.hostname_count === 1 ? '' : 's'}
        </Anchor>
      <span>
        {nowhere ? <ZoneNowhereMark testId={`nowhere-${domain.name}`} /> : null}
      </span>
      {nowhere ? (
        <div className="ddns-zones__note">
          <ZoneWireConsequence testId={`nowhere-why-${domain.name}`} />
        </div>
      ) : null}
    </div>
  );
}

export function DomainList({
  domains,
  total,
  onOpen,
}: {
  domains: Domain[];
  /** Before filtering. Lets an empty result say *the search matched
   *  nothing* rather than *you have no zones*, which are different
   *  facts and only one of them is about the account. */
  total: number;
  /** Navigates. The list does not know that opening a zone is a URL
   *  change — `DomainsPage` owns that, so there is one place that
   *  decides what a zone address is. */
  onOpen: (id: number) => void;
}) {
  if (total === 0) {
    return (
      <Text size="sm" data-testid="domains-empty">
        You have no zones yet. Add one — a zone needs a DNS provider to publish
        through, and the form asks for both.
      </Text>
    );
  }
  if (domains.length === 0) {
    return (
      <Text size="sm" data-testid="domains-no-match">
        No zone matches that search. {total} zone{total === 1 ? '' : 's'} in
        total.
      </Text>
    );
  }
  return (
    <div className="ddns-zones" data-testid="domains-table">
      {/* A grid, not a stack of flex rows. Every row shares the same
          column template, so the provider and the name count line up
          down the page instead of each row deciding its own widths from
          the length of its zone name. */}
      <div className="ddns-zones__head">
        <span className="ddns-th">Zone</span>
        <span className="ddns-th">Provider</span>
        <span className="ddns-th">Names</span>
        <span className="ddns-th" />
      </div>
      {domains.map((domain, index) => (
        <ZoneRow
          key={domain.id}
          domain={domain}
          index={index}
          onOpen={onOpen}
        />
      ))}
    </div>
  );
}
