/** The credential shown once, and the two ways of getting it off screen.
 *
 * A secret that is displayed exactly once and cannot be recovered puts
 * unusual weight on the copy step: a selection that clips a character
 * produces a device that authenticates today and fails whenever the
 * router next re-reads its config, and by then the value is gone. So the
 * copy controls are asserted to exist rather than assumed — and asserted
 * beside the values they copy, because a copy button wired to the wrong
 * string is the failure this cannot recover from.
 */
import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MantineProvider } from '@mantine/core';
import { SecretOnce } from '../tenant/SecretOnce';

describe('the issued credential offers to copy both values', () => {
  test('a copy control beside the username and beside the secret', () => {
    render(
      <MantineProvider>
        <SecretOnce
          issued={{
            device: { id: 1, name: 'r', username: 'ddns-abc', origin: 'argon2id' },
            secret: 's3cr3t-value',
          } as never}
          onDismiss={() => {}}
        />
      </MantineProvider>,
    );
    expect(screen.getByTestId('copy-username')).toBeInTheDocument();
    expect(screen.getByTestId('copy-secret')).toBeInTheDocument();
    // Non-vacuous: the values they copy are the ones on screen.
    expect(screen.getByTestId('issued-username')).toHaveTextContent('ddns-abc');
    expect(screen.getByTestId('issued-secret')).toHaveTextContent('s3cr3t-value');
  });
});
