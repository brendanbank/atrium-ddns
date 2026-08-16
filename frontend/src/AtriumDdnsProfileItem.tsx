import { Card, Stack, Text, Title } from '@mantine/core';
import { useMe } from '@brendanbank/atrium-host-bundle-utils/react';

import { DdnsRoot } from './host/DdnsRoot';

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
  return (
    <DdnsRoot>
      <AtriumDdnsProfileItemInner />
    </DdnsRoot>
  );
}
