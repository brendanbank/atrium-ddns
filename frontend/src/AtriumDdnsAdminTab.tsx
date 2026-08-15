import { MantineProvider, Stack, Text, Title } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import {
  AtriumProvider,
  useAtriumColorScheme,
} from '@brendanbank/atrium-host-bundle-utils/react';

import { AtriumDdnsWidget } from './AtriumDdnsWidget';
import { queryClient } from './queryClient';

function AtriumDdnsAdminTabInner() {
  return (
    <Stack gap="md">
      <Title order={3}>Atrium Ddns admin</Title>
      <Text c="dimmed" size="sm">
        Permission-gated tab in the admin shell. Atrium hides this tab
        for users without ``atrium_ddns.write``; the API enforces the
        same on every write.
      </Text>
      <AtriumDdnsWidget />
    </Stack>
  );
}

export function AtriumDdnsAdminTab() {
  const scheme = useAtriumColorScheme();
  return (
    <MantineProvider defaultColorScheme={scheme}>
      <QueryClientProvider client={queryClient}>
        <AtriumProvider>
          <AtriumDdnsAdminTabInner />
        </AtriumProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}
