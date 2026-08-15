import { Card, MantineProvider, Stack, Text, Title } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import {
  AtriumProvider,
  useAtriumColorScheme,
  useMe,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { queryClient } from './queryClient';

function AtriumDdnsProfileItemInner() {
  const { data: me } = useMe();
  return (
    <Card withBorder padding="lg" radius="md">
      <Stack gap="xs">
        <Title order={4}>Atrium Ddns</Title>
        <Text size="sm" c="dimmed">
          Per-user host extension card slotted after the role list.
          Use this slot for app-specific identity bits — preferred
          location, notification preferences, opt-in features.
        </Text>
        {me && (
          <Text size="sm">
            Signed in as <strong>{me.email}</strong>.
          </Text>
        )}
      </Stack>
    </Card>
  );
}

export function AtriumDdnsProfileItem() {
  const scheme = useAtriumColorScheme();
  return (
    <MantineProvider defaultColorScheme={scheme}>
      <QueryClientProvider client={queryClient}>
        <AtriumProvider>
          <AtriumDdnsProfileItemInner />
        </AtriumProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}
