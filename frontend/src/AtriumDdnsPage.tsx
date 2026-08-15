import { Container, Stack, Text, Title } from '@mantine/core';
import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import {
  AtriumProvider,
  useAtriumColorScheme,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { AtriumDdnsWidget } from './AtriumDdnsWidget';
import { queryClient } from './queryClient';

function AtriumDdnsPageInner() {
  return (
    <Container size="md" py="xl">
      <Stack gap="md">
        <Title order={2}>Atrium Ddns</Title>
        <Text c="dimmed">
          Dedicated route registered by the host bundle. Replace this
          page with your real domain UI.
        </Text>
        <AtriumDdnsWidget />
      </Stack>
    </Container>
  );
}

export function AtriumDdnsPage() {
  const scheme = useAtriumColorScheme();
  return (
    <MantineProvider defaultColorScheme={scheme}>
      <QueryClientProvider client={queryClient}>
        <AtriumProvider>
          <AtriumDdnsPageInner />
        </AtriumProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}
