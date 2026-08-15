/** Home-widget rendering of the demo state.
 *
 * Wraps itself in `<DdnsRoot>`, which owns the MantineProvider +
 * QueryClientProvider + AtriumProvider stack and the three props that
 * keep a nested Mantine provider from restyling atrium's shell. See
 * `host/DdnsRoot.tsx` — the stack used to be repeated here and in three
 * other files, which is three places to get each of them wrong.
 *
 * Permission gating uses `usePerm()` from
 * `@brendanbank/atrium-host-bundle-utils/react` — a single TanStack
 * Query subscription against atrium's `/users/me/context`, shared
 * across this widget, the dedicated page, and the admin tab.
 */
import {
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import {
  bumpAtriumDdns,
  getAtriumDdnsState,
  type AtriumDdnsState,
} from './api';
import { DdnsRoot } from './host/DdnsRoot';

const STATE_KEY = ['atrium_ddns', 'state'] as const;

function AtriumDdnsWidgetInner() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: STATE_KEY,
    queryFn: getAtriumDdnsState,
  });
  const hasPerm = usePerm();
  const canBump = hasPerm('atrium_ddns.write');
  const bumpMutation = useMutation({
    mutationFn: bumpAtriumDdns,
    onSuccess: (next: AtriumDdnsState) => {
      qc.setQueryData(STATE_KEY, next);
    },
  });

  return (
    <Card withBorder padding="lg" radius="md" data-testid="atrium-ddns-card">
      <Stack gap="sm">
        <Group justify="space-between" align="center">
          <Title order={4}>Atrium Ddns</Title>
          <Badge color={canBump ? 'teal' : 'gray'} variant="light">
            {canBump ? 'editor' : 'viewer'}
          </Badge>
        </Group>
        {isLoading && (
          <Group gap="xs">
            <Loader size="xs" />
            <Text size="sm" c="dimmed">Loading…</Text>
          </Group>
        )}
        {error && (
          <Text c="red" size="sm">
            Error: {(error as Error).message}
          </Text>
        )}
        {data && (
          <>
            <Text size="lg" fw={500} data-testid="atrium-ddns-message">
              {data.message}
            </Text>
            <Text
              ff="monospace"
              size="xl"
              fw={700}
              data-testid="atrium-ddns-counter"
            >
              {data.counter}
            </Text>
            <Button
              onClick={() => bumpMutation.mutate()}
              disabled={!canBump || bumpMutation.isPending}
              data-testid="atrium-ddns-bump"
              fullWidth
            >
              {canBump ? 'Bump counter' : 'Bump (admin only)'}
            </Button>
          </>
        )}
      </Stack>
    </Card>
  );
}

export function AtriumDdnsWidget() {
  return (
    <DdnsRoot>
      <AtriumDdnsWidgetInner />
    </DdnsRoot>
  );
}
