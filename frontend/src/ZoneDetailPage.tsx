/** `/atrium-ddns/zones/:id` — the zone, with its providers inside it.
 *
 * ## The route is one of two entrances, and it is the one you can paste
 *
 * §12 chose a route over a drawer and a split pane on a width budget.
 * **Part III §17 records the operator overruling it**: they asked twice
 * for a modal that pops up, so clicking a row on `/domains` now opens
 * `ZoneCard` in a modal. What that overturns is narrower than it looks,
 * and §17 says so rather than leaving it to be re-argued:
 *
 * - The width argument was measured against a Mantine `lg` drawer
 *   (620px) and a 360/800 split (~790px). A `Modal` takes an arbitrary
 *   `size`, so it can carry §3.1's 592px comfortably. §12 rejected two
 *   shapes and never evaluated the third.
 * - **Linkability and Back survived on their own merits**, and both are
 *   properties of a URL rather than of a container. So this route stays.
 *
 * What it renders is `ZoneCard` — the same module `DomainList` opens in
 * its modal, imported and not copied. `src/test/sharedCard.test.tsx`
 * asserts that by module identity: it substitutes the module and checks
 * the substitute reaches both entrances.
 *
 * ## Reading the id
 *
 * `useParams` is not available here. Atrium owns the router context and
 * `makeWrapperElement` mounts this bundle in a **second React root**, so
 * react-router's context does not cross the boundary. `useAtriumLocation`
 * is the supported bridge — it subscribes to atrium's own
 * `atrium:locationchange` event — and `zoneIdFromPath` is the only place
 * the pathname is parsed.
 *
 * ## What is left in this file
 *
 * The back link, and one state the card cannot have: **the URL is not a
 * zone address**. That is a fact about the pathname, and a modal opened
 * from a row has no pathname to be wrong about. Every other state —
 * refused, loading, failed, and "zone N is not one of yours" — belongs
 * to the card, because both entrances have them.
 */
import { Alert, Anchor, Stack, Text } from '@mantine/core';
import { useAtriumLocation } from '@brendanbank/atrium-host-bundle-utils/react';

import { DdnsRoot } from './host/DdnsRoot';
import { DOMAINS_PATH, zoneIdFromPath } from './paths';
import { ZoneCard } from './tenant/ZoneCard';

export function ZoneDetailInner() {
  const { pathname } = useAtriumLocation();
  const zoneId = zoneIdFromPath(pathname);

  return (
    <Stack gap="lg">
      <Anchor href={DOMAINS_PATH} size="sm" data-testid="zone-back">
        ← zones
      </Anchor>

      {zoneId === null ? (
        // A fact about the URL, not about the tenant's data. Distinct
        // from "no such zone", which is a fact about the account.
        <Alert
          color="gray"
          variant="light"
          title="That is not a zone address"
          data-testid="zone-bad-url"
        >
          <Text size="sm" ff="monospace">
            {pathname}
          </Text>
        </Alert>
      ) : (
        <ZoneCard zoneId={zoneId} />
      )}
    </Stack>
  );
}

export function ZoneDetailPage() {
  return (
    <DdnsRoot>
      <ZoneDetailInner />
    </DdnsRoot>
  );
}
