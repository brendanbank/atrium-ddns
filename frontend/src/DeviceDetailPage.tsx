/** `/atrium-ddns/devices/:id` — `docs/ops/ui-design.md` §11.2.
 *
 * ## The route is one of two entrances, and it is the one you can paste
 *
 * §12 chose a route on the width budget: one resolution strip needs
 * ≈592px (§3.1), a 360/800 split leaves ~790px, and Mantine's `lg`
 * drawer is 620px — *below the one-strip minimum*, so the signature
 * element would wrap inside its own detail view.
 *
 * **Part III §17 records the operator overruling that.** They asked
 * twice for a modal, so a click on a device row — on the list *and* on
 * the board — now opens `DeviceCard` in a modal sized from the same
 * measurement (`cards.ts`). What §17 does *not* overturn is this route:
 * linkability and Back survived on their own merits, and both are
 * properties of a URL rather than of a container. An operator still
 * pastes a device URL into a ticket, and it still resolves — to the same
 * card, from the same module, which `src/test/sharedCard.test.tsx`
 * asserts by substituting that module and checking the substitute
 * reaches both entrances.
 *
 * ## What is left in this file
 *
 * The back link, and one state the card cannot have: **the URL is not a
 * device address**. That is a fact about the pathname; a modal opened
 * from a row has no pathname to be wrong about. Refused, loading, "no
 * such device" and the device itself all belong to `DeviceCard`, because
 * both entrances have them.
 */
import { useEffect, useState } from 'react';
import { Alert, Anchor, Stack, Text } from '@mantine/core';

import { DdnsRoot } from './host/DdnsRoot';
import { DEVICES_PATH, deviceIdFromPath } from './paths';
import { DeviceCard } from './tenant/DeviceCard';

/** The id in the address bar.
 *
 * `window.location`, not `useParams`: the host bundle mounts its own
 * React tree inside atrium's, so react-router's context does not cross
 * the boundary and importing react-router here would give this tree a
 * *second* router with its own idea of the location. Same decision, and
 * the same reasoning, as `logs/useLogQuery.ts`.
 *
 * Guarded for a non-browser host — a throw at module scope would take
 * the whole bundle down at import time.
 */
function useDeviceId(): number | null {
  const read = () =>
    typeof window === 'undefined'
      ? null
      : deviceIdFromPath(window.location.pathname);
  const [id, setId] = useState<number | null>(read);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onPop = () => setId(read());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return id;
}

export function DeviceDetailInner() {
  const id = useDeviceId();

  return (
    <Stack gap="md">
      <Anchor href={DEVICES_PATH} size="sm" data-testid="detail-back">
        {/* A real anchor, for `DeviceBoardPage`'s reason: react-router's
            `Link` is not reachable from this tree and a bare `pushState`
            would move the address bar without telling atrium's router. */}
        ← devices
      </Anchor>

      {id === null ? (
        <Alert
          color="gray"
          variant="light"
          title="That is not a device address"
          data-testid="detail-bad-url"
        >
          <Text size="sm">
            <code>{DEVICES_PATH}/&lt;id&gt;</code> expects a numeric id. Go back
            to the device list and follow a row.
          </Text>
        </Alert>
      ) : (
        <DeviceCard deviceId={id} />
      )}
    </Stack>
  );
}

export function DeviceDetailPage() {
  return (
    <DdnsRoot>
      <DeviceDetailInner />
    </DdnsRoot>
  );
}
