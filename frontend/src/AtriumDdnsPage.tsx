import { Container, Stack, Text, Title } from '@mantine/core';

import { AtriumDdnsWidget } from './AtriumDdnsWidget';
import { DdnsRoot } from './host/DdnsRoot';

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
  return (
    <DdnsRoot>
      <AtriumDdnsPageInner />
    </DdnsRoot>
  );
}
