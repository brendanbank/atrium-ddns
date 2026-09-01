/** Creating a device, as a modal any surface can host.
 *
 * It lived inside `DeviceList`, which is why `/atrium-ddns/devices` had to
 * exist: the board could offer *"Add a device"* only by sending you to a
 * page that owned the form, and that page had no nav entry, so finishing
 * left you somewhere you could not navigate away from. Extracting it is
 * what lets the board be the only tenant surface.
 *
 * ## Why the secret modal is part of this and not the caller's problem
 *
 * Creating a device answers with a credential shown **once**. Two things
 * follow, and both were learned rather than designed:
 *
 * - **The form stays open behind the secret.** Closing it the instant the
 *   secret appeared put the one string that can never be recovered on a
 *   page that had just moved under the pointer. A modal over a modal says
 *   *you are not finished yet*, and the form behind it is the context for
 *   what you are being handed.
 * - **Dismissing the secret closes both.** One exit, so there is no state
 *   where the credential is gone and the form is still up asking to
 *   create another.
 *
 * That coupling is the whole reason this is one component. A caller that
 * held the form and left the secret to `SecretOnceModal` separately would
 * have to reproduce both rules, and the second one silently.
 *
 * `SecretOnceModal` is shared with rotation — same object, shown the same
 * way, with a title saying which operation produced it.
 */
import { useState } from 'react';
import {
  Alert,
  Button,
  Group,
  Modal,
  NumberInput,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import { BOARD_QUERY_KEY } from '../api/board';
import {
  DEVICES_QUERY_KEY,
  createDevice,
  type DeviceSecret,
} from '../api/devices';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { SecretOnceModal } from './SecretOnce';

export function DeviceCreateModal({
  opened,
  onClose,
}: {
  opened: boolean;
  /** Called once the whole flow is finished — after the secret has been
   *  dismissed, not when the device is created. The caller closes its
   *  address; this component decides when there is nothing left to show. */
  onClose: () => void;
}) {
  const client = useQueryClient();
  // The secret lives here and nowhere else. Not in the query cache, not
  // in storage, not in a ref that survives a remount.
  const [issued, setIssued] = useState<DeviceSecret | null>(null);
  const [name, setName] = useState('');
  const [limit, setLimit] = useState<number | ''>('');
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: createDevice,
    onSuccess: (result) => {
      setIssued(result);
      setError(null);
      // Both lists. The board is the surface this is opened from and the
      // devices query is what the card reads; invalidating one leaves the
      // other a device behind.
      void client.invalidateQueries({ queryKey: DEVICES_QUERY_KEY });
      void client.invalidateQueries({ queryKey: BOARD_QUERY_KEY });
    },
    onError: (err: Error) => setError(err.message),
  });

  /** One exit for the whole flow: drop the secret, clear the form, and
   *  tell the caller. Called from the secret's dismiss and from the
   *  form's own close, so there is exactly one path out. */
  const finish = () => {
    setIssued(null);
    setName('');
    setLimit('');
    setError(null);
    onClose();
  };

  return (
    <>
      <Modal
        opened={opened}
        onClose={finish}
        title="Add a device"
        data-testid="device-create-modal"
      >
        <DdnsPortalScope>
          <Stack gap="sm">
            <TextInput
              label="Name"
              value={name}
              disabled={create.isPending}
              onChange={(event) => setName(event.currentTarget.value)}
              data-testid="device-name"
            />
            <NumberInput
              label="Rate limit (per minute)"
              placeholder="inherit"
              value={limit}
              min={0}
              disabled={create.isPending}
              onChange={(value) =>
                setLimit(typeof value === 'number' ? value : '')
              }
              data-testid="device-limit"
            />
            {error ? (
              <Alert
                color="gray"
                variant="light"
                title="That did not work"
                data-testid="device-error"
              >
                <Text size="sm" ff="monospace">
                  {error}
                </Text>
              </Alert>
            ) : null}
            <Group justify="flex-end" gap="sm">
              <Button
                size="xs"
                variant="default"
                disabled={create.isPending}
                onClick={finish}
                data-testid="device-create-cancel"
              >
                Cancel
              </Button>
              <Button
                size="xs"
                disabled={name.trim() === '' || create.isPending}
                onClick={() =>
                  create.mutate({
                    name: name.trim(),
                    // `null`, not `0`. An empty box means *inherit*, and
                    // coercing it to zero would mute the device.
                    rate_limit_per_minute: limit === '' ? null : limit,
                  })
                }
                data-testid="device-submit"
              >
                Add
              </Button>
            </Group>
          </Stack>
        </DdnsPortalScope>
      </Modal>

      <SecretOnceModal issued={issued} onDismiss={finish} />
    </>
  );
}
