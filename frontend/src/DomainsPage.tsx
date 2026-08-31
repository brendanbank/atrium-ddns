/** Zones and providers — the list, and the modal drawn over it.
 *
 * ## The modal is the URL
 *
 * One route, and `?zone=` decides whether a modal is over it. Reload,
 * Back and a pasted link all work, because the address carries the
 * state and nothing else remembers it.
 *
 * **It was two routes and that was a bug.** `/atrium-ddns/zones/:id` was
 * registered separately, so opening and closing swapped atrium's route
 * element — which unmounts the host's React root. The modal is portalled
 * to `document.body`, so closing orphaned the portal: the zone was
 * deleted, the list refreshed underneath, and the dialog stayed on
 * screen. A query parameter never changes the route, so the mount and
 * its portal survive.
 *
 * **An empty `main` on this page is not evidence of that bug coming
 * back.** #122 looked exactly like it from the other side — dialog and
 * list gone together mid-interaction, the landmark empty — and no route
 * swapped. `ZoneModal`'s credential field read
 * `event.currentTarget.value` from inside a functional `setState`, which
 * React defers to the render phase whenever an update is already
 * pending; the synthetic event is detached by then, the read throws
 * *during render*, and with no error boundary over the host tree React
 * unmounts the whole root. Same picture, unrelated cause — so read the
 * page's uncaught-error channel before suspecting the address bar.
 * `src/test/eventInUpdater.test.ts` is the guard that keeps that
 * spelling out of the tree.
 *
 * ## Four states, not two
 *
 * *Refused*, *loading*, *failed* and *loaded* are different, and the
 * refusal branch fires no query — so a user without the permission does
 * not generate a 403 on every page load. Telling a refused user "you
 * have no zones yet" states a fact about their account that is untrue.
 */
import { useState } from 'react';
import { Alert, Button, Group, Stack, Text, TextInput, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import {
  useAtriumLocation,
  useAtriumNavigate,
  usePerm,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { DOMAIN_PERMISSION, domainsQuery } from './api/domains';
import { DdnsRoot } from './host/DdnsRoot';
import { DomainList } from './tenant/DomainList';
import { ZoneModal } from './tenant/ZoneModal';
import {
  ZONES_LIST_PATH,
  zoneFromSearch,
  zoneHrefParam,
  zoneNewHref,
} from './paths';

export function DomainsInner() {
  const hasPerm = usePerm();
  const canRead = hasPerm(DOMAIN_PERMISSION);
  const domains = useQuery(domainsQuery({ enabled: canRead }));
  const [query, setQuery] = useState('');
  const { search } = useAtriumLocation();
  const navigate = useAtriumNavigate();

  /** The modal's state, read from `?zone=`. Never mirrored into
   *  `useState`: two sources of truth is how the modal and the address
   *  bar came to disagree in the first place. */
  const zone = zoneFromSearch(search);
  const modalOpen = zone.open;
  const zoneId = zone.open ? zone.id : null;
  /** Closing drops the parameter. `replace` so opening and closing does
   *  not leave a Back step that reopens it. */
  const closeModal = () => navigate(ZONES_LIST_PATH, { replace: true });

  /** Filtering starts at two characters, as asked. One character matches
   *  most of the list and is more likely a keystroke on the way to a
   *  real query than a query; below the threshold the list is whole
   *  rather than empty, so the field never looks broken while you type.
   *  Matches the zone name and the provider, because those are the two
   *  columns you can see. */
  const needle = query.trim().toLowerCase();
  const filtered =
    needle.length < 2
      ? (domains.data ?? [])
      : (domains.data ?? []).filter(
          (d) =>
            d.name.toLowerCase().includes(needle) ||
            (d.backends[0]?.backend_type ?? '').toLowerCase().includes(needle),
        );

  return (
    <Stack gap="md">
      <Group justify="space-between" align="center">
        <Title order={3}>Zones and providers</Title>
        {/* "Manage names" is gone from here: every row links to the
            names for its own zone, which is the useful version of the
            same trip, and a second global link beside the primary action
            competed with it for the same corner. */}
        {canRead ? (
          <Group gap="sm" align="flex-end">
            <Button
              size="xs"
              onClick={() => navigate(zoneNewHref())}
              data-testid="add-domain"
            >
              Add a zone
            </Button>
            <TextInput
              size="xs"
              label="Search"
              placeholder="zone or provider"
              value={query}
              onChange={(event) => setQuery(event.currentTarget.value)}
              data-testid="domains-search"
              style={{ width: 220 }}
            />
          </Group>
        ) : null}
      </Group>

      {!canRead ? (
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="domains-refused"
        >
          <Text size="sm">
            Managing zones needs the <code>{DOMAIN_PERMISSION}</code> permission.
            This is a refusal, not an empty list — ask an administrator for the
            permission rather than assuming you own no zones.
          </Text>
        </Alert>
      ) : domains.isLoading ? (
        <Text size="sm" data-testid="domains-loading">
          Loading…
        </Text>
      ) : domains.error ? (
        <Alert
          color="gray"
          variant="light"
          title="Could not load your zones"
          data-testid="domains-error"
        >
          <Text size="sm" ff="monospace">
            {(domains.error as Error).message}
          </Text>
        </Alert>
      ) : domains.data ? (
        <DomainList
          domains={filtered}
          total={domains.data.length}
          onOpen={(id) => navigate(zoneHrefParam(id))}
        />
      ) : null}

      {/* One modal for both jobs, and its open-ness is the address bar.
          `zoneId` is `null` on the create address, which is what
          `ZoneModal` reads as *create*. */}
      <ZoneModal zoneId={zoneId} opened={modalOpen} onClose={closeModal} />
    </Stack>
  );
}

export function DomainsPage() {
  return (
    <DdnsRoot>
      <DomainsInner />
    </DdnsRoot>
  );
}
