import { Stack, Text, Title } from '@mantine/core';

import { AtriumDdnsWidget } from './AtriumDdnsWidget';
import { DdnsRoot } from './host/DdnsRoot';

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
  return (
    <DdnsRoot>
      <AtriumDdnsAdminTabInner />
    </DdnsRoot>
  );
}
