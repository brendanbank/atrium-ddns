/** The one moment a device secret is visible, and the sentence that
 *  says so.
 *
 * The secret is **hashed, not encrypted** (`docs/ops/refactor-plan.md`
 * §3.2). There is no key that recovers it, no admin screen that reveals
 * it and no support procedure that produces it: create and rotate are
 * the only two moments it exists in cleartext anywhere, and this
 * component is what stands at both of them.
 *
 * ## Three things this deliberately does not do
 *
 * **It does not persist.** Not to `localStorage`, not to the query
 * cache, not to a ref that survives a remount. Anything that kept it
 * would be building a second and worse copy of the thing the database
 * refuses to keep — worse because nobody would think to protect it, and
 * because it would make "shown once" false without changing a word of
 * the interface.
 *
 * **It does not soften the sentence.** "Copy this now" is advice. *"This
 * is the only time this secret will ever be shown"* is a fact, and a
 * user who reads advice and skips it has to rotate — invalidating a
 * working router in the process. The sentence is the interface's whole
 * job here.
 *
 * **It does not offer a “show again”.** Not disabled, not greyed out,
 * not present. A disabled control for an impossible operation teaches
 * that the operation exists and is merely unavailable, which is the
 * belief that later becomes a support request.
 *
 * ## The migrated device
 *
 * A device carrying a bcrypt hash came from the old service and **we
 * never held its plaintext**. `MigratedNotice` says that in as many
 * words rather than rendering an empty secret field: an empty field
 * reads as *something is missing here*, when the truth is *nothing was
 * ever here, and rotating is the only way to get one*. The distinction
 * matters because the two states want opposite actions — one is a bug
 * report, the other is a decision about whether to reconfigure a router.
 */
import { Alert, Button, Code, Group, Stack, Text } from '@mantine/core';

import type { CredentialOrigin, DeviceSecret } from '../api/devices';

/** What the interface says about a stored secret, by origin.
 *
 * A total map over `CredentialOrigin`, so a fourth value added
 * server-side stops compiling here rather than falling through to a
 * default that says something untrue about it.
 */
export const ORIGIN_NOTICE: Record<CredentialOrigin, string | null> = {
  // Nothing to say: it was shown once, at the moment it was issued, and
  // the interface said so then. Repeating it on every render would make
  // the sentence furniture.
  issued: null,
  migrated:
    'This device was migrated from the old service. Its secret was hashed there, ' +
    'so we have never held the plaintext and cannot show it — not now and not later. ' +
    'The router that already has it keeps working. Rotate only if you have lost it, ' +
    'and expect to reconfigure the router.',
  unrecognised:
    'This device’s stored credential is in a format no verifier recognises, so it ' +
    'cannot authenticate at all. Rotate to issue a working secret.',
};

export function MigratedNotice({ origin }: { origin: CredentialOrigin }) {
  const notice = ORIGIN_NOTICE[origin];
  if (notice === null) return null;
  return (
    <Alert
      color="gray"
      variant="light"
      data-testid={`device-origin-${origin}`}
      title={
        origin === 'migrated'
          ? 'No secret to show'
          : 'This credential cannot authenticate'
      }
    >
      <Text size="sm">{notice}</Text>
    </Alert>
  );
}

/** The credential pair, shown once.
 *
 * The username is rendered beside the secret because they are one
 * credential and are configured into the router together — handing back
 * a secret whose username the user then has to go and find is a
 * two-screen operation for a one-screen fact.
 */
export function SecretOnce({
  issued,
  onDismiss,
}: {
  issued: DeviceSecret;
  onDismiss: () => void;
}) {
  return (
    <Alert
      color="gray"
      variant="light"
      title="Copy this secret now"
      data-testid="device-secret-once"
    >
      <Stack gap="xs">
        <Text size="sm" data-testid="secret-once-warning">
          This is the only time this secret will ever be shown. It is stored as a
          hash, so it cannot be recovered — if you lose it you must rotate, which
          issues a new one and stops the old one working.
        </Text>
        <div className="ddns-strip__stations">
          <span className="ddns-th">Username</span>
          <span className="ddns-data" data-testid="issued-username">
            {issued.device.username}
          </span>
          <span />
          <span className="ddns-th">Secret</span>
          <Code className="ddns-data" data-testid="issued-secret">
            {issued.secret}
          </Code>
          <span />
        </div>
        <Group justify="flex-end">
          <Button
            size="xs"
            variant="default"
            onClick={onDismiss}
            data-testid="dismiss-secret"
          >
            I have copied it
          </Button>
        </Group>
      </Stack>
    </Alert>
  );
}
