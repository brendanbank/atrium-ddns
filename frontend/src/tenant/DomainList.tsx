/** The zone list — one row per zone, and the create flow.
 *
 * ## What #88 changed, and why it is not a preference
 *
 * Before: one row per zone with its provider bindings nested under it,
 * and a create modal offering a single text box and a Create button.
 *
 * That modal manufactures a **zero-provider zone** in one click. Part II
 * §8.1 measures what that is: every update for a name under such a zone
 * answers `911`, frozen at `tests/compat/protocol_cases.yaml:211`
 * (`update/no-backends-911`), which is the DynDNS v2 code for *the
 * service is broken, stop asking*. Then the list drew it identically to
 * a working zone — the exceptional row in the same ink as the other 500,
 * which is the precise inverse of §0's thesis.
 *
 * Three consequences, all of them here:
 *
 * 1. **The create form asks for the zone and its first provider**, in
 *    one submission, reusing `BackendForm` rather than a second copy of
 *    the credential rules. One request, one transaction: the server
 *    creates both rows or neither.
 * 2. **"Add a provider later" is a link, not a checkbox, and not the
 *    default.** The path stays open — staging a migration is a real
 *    reason — but it is a deliberate act with the consequence written
 *    next to it.
 * 3. **A zone with no provider renders diverged**, in the operator's
 *    terms rather than the protocol's. `ZoneStatus` owns the words.
 *
 * ## The bindings moved out of this file
 *
 * §10.2: providers are listed *inside* the zone, because that is where
 * they live. They were nested here on a shared list page, which is the
 * same information one level too deep and three clicks from the thing it
 * describes. `ZoneDetailPage` is now the only surface that edits them,
 * so there is one rendering of a binding rather than two that can
 * disagree.
 */
import { useState } from 'react';
import { Alert, Anchor, Button, Group, Modal, Stack, Text } from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  DOMAINS_QUERY_KEY,
  createDomain,
  type Domain,
  type Provider,
} from '../api/domains';
import { opensInThisTab } from '../cards';
import { zoneHref } from '../paths';
import { BackendForm } from './BackendForm';
import { ZoneCardModal } from './ZoneCard';
import {
  ZoneLaterConsequence,
  ZoneNowhereMark,
  ZoneWireConsequence,
} from './ZoneStatus';

/** One zone, as the list draws it.
 *
 * `data-diverged` carries the treatment rather than a class name, so the
 * stylesheet expresses "a zone with no provider is diverged" as one rule
 * instead of the component deciding which class means what.
 */
function ZoneRow({
  domain,
  onOpen,
}: {
  domain: Domain;
  onOpen: (id: number) => void;
}) {
  const nowhere = domain.backends.length === 0;
  return (
    <div
      className="ddns-zone"
      data-diverged={nowhere ? 'true' : 'false'}
      data-testid={`domain-${domain.name}`}
    >
      <div className="ddns-zone__head">
        {/* Still a link, and still an `href`. Part III §17 keeps §12's
            two surviving arguments — linkability and Back — because
            both are properties of the URL, and an operator pasting a
            zone address into a ticket is the case they name. What
            changed is what a *plain* click does: it opens the card in a
            modal, which is what the operator asked for twice. A
            cmd/ctrl/shift/middle click is not intercepted and still
            navigates, so "open in a new tab" and "copy link address"
            behave exactly as the anchor promises.

            The `.ddns-data` class stays — §2.3, the type idea, on the
            most important string on the page — and `ddns.css` §2a now
            gives an interactive `.ddns-data` an underline **at rest**.
            That is §16.1's fix: colour is spoken for, so the affordance
            is not colour, and hover-only would fail the same operator
            on the same page and fail entirely on touch. */}
        <Anchor
          href={zoneHref(domain.id)}
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
        <span className="ddns-label">
          {domain.hostname_count} name{domain.hostname_count === 1 ? '' : 's'}
        </span>
        <span className="ddns-label" data-testid={`providers-${domain.name}`}>
          {domain.backends.length} provider
          {domain.backends.length === 1 ? '' : 's'}
        </span>
        {nowhere ? (
          <ZoneNowhereMark testId={`nowhere-${domain.name}`} />
        ) : null}
      </div>
      {nowhere ? (
        <ZoneWireConsequence testId={`nowhere-why-${domain.name}`} />
      ) : null}
    </div>
  );
}

export function DomainList({
  domains,
  providers,
}: {
  domains: Domain[];
  providers: Provider[];
}) {
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [zoneName, setZoneName] = useState('');
  const [error, setError] = useState<string | null>(null);
  /** The "add a provider later" confirmation. Two states and not one:
   *  the link opens a sentence and a button, so taking the path is two
   *  deliberate acts rather than one stray click on a modal that was
   *  already open. */
  const [confirmLater, setConfirmLater] = useState(false);
  /** Which zone's card is open, if any. `null` is *closed* and not
   *  *zone zero*: the id goes straight into `ZoneCard`'s lookup. */
  const [openZone, setOpenZone] = useState<number | null>(null);

  const invalidate = () =>
    client.invalidateQueries({ queryKey: DOMAINS_QUERY_KEY });
  const fail = (err: Error) => setError(err.message);
  const done = () => {
    setError(null);
    setAdding(false);
    setZoneName('');
    setConfirmLater(false);
    void invalidate();
  };

  const addZone = useMutation({
    mutationFn: createDomain,
    onSuccess: done,
    // The server's refusal, verbatim. A zone name another tenant already
    // claims is a 409 whose detail says so, and a credential the adapter
    // will not take is a 422 naming the field — both of which the
    // operator can act on, so neither is reworded here.
    onError: fail,
  });

  const busy = addZone.isPending;
  const trimmedZone = zoneName.trim();

  return (
    <Stack gap="lg">
      {error ? (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="domain-error"
        >
          <Text size="sm" ff="monospace">
            {error}
          </Text>
        </Alert>
      ) : null}

      {domains.length === 0 ? (
        <Text size="sm" data-testid="domains-empty">
          You have no zones yet. Add one — a zone needs a DNS provider to
          publish through, and the form asks for both.
        </Text>
      ) : (
        domains.map((domain) => (
          <ZoneRow key={domain.id} domain={domain} onOpen={setOpenZone} />
        ))
      )}

      {/* §17's normal entrance. The same `ZoneCard` the route renders —
          one definition, two call sites, asserted by module identity in
          `src/test/sharedCard.test.tsx`. */}
      <ZoneCardModal zoneId={openZone} onClose={() => setOpenZone(null)} />

      <Group>
        <Button size="xs" onClick={() => setAdding(true)} data-testid="add-domain">
          Add a zone
        </Button>
      </Group>

      <Modal
        opened={adding}
        onClose={() => {
          setAdding(false);
          setConfirmLater(false);
        }}
        title="Add a zone"
      >
        {/* `BackendForm`, not a copy of it. The credential mode selector,
            `buildCredentialsPayload` and the server-derived field list
            are one implementation of one rule; a create-only fork would
            look harmless (a new zone has nothing stored, so two of the
            three modes are unreachable) and would pass its own tests on
            the day it was written. */}
        <BackendForm
          providers={providers}
          busy={busy}
          submitLabel="Add"
          submitDisabled={trimmedZone === ''}
          header={
            <Stack gap={4}>
              <Text size="sm" fw={500}>
                Zone
              </Text>
              <input
                className="ddns-data"
                aria-label="Zone name"
                value={zoneName}
                onChange={(event) => setZoneName(event.currentTarget.value)}
                data-testid="zone-name"
              />
              <Text size="xs" c="dimmed">
                Stored lower-cased and without a trailing dot. Zone names are
                unique across the whole installation — DNS is global — so a
                name someone else already runs is refused.
              </Text>
            </Stack>
          }
          footer={
            <Stack gap={4} data-testid="zone-later">
              {/* A link, not a checkbox, and not the default (§10.1).
                  A checkbox sits in the form's reading order as one more
                  option; a link is a departure from it, and this one has
                  its consequence attached before it can be taken. */}
              <Anchor
                component="button"
                type="button"
                size="xs"
                onClick={() => setConfirmLater((current) => !current)}
                data-testid="zone-later-link"
              >
                Add a provider later
              </Anchor>
              {confirmLater ? (
                <Stack gap={4}>
                  <ZoneLaterConsequence testId="zone-later-warning" />
                  <Group>
                    <Button
                      size="xs"
                      variant="default"
                      disabled={busy || trimmedZone === ''}
                      onClick={() =>
                        addZone.mutate({ name: trimmedZone, backend: null })
                      }
                      data-testid="zone-later-submit"
                    >
                      Create it with no provider
                    </Button>
                  </Group>
                </Stack>
              ) : null}
            </Stack>
          }
          onSubmit={(value) =>
            addZone.mutate({ name: trimmedZone, backend: value })
          }
        />
      </Modal>
    </Stack>
  );
}
