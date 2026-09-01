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
import {
  ActionIcon,
  Alert,
  Button,
  Code,
  CopyButton,
  Group,
  Modal,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { IconCheck, IconCopy } from '@tabler/icons-react';

import type { CredentialOrigin, DeviceSecret } from '../api/devices';
import { DdnsPortalScope } from '../host/DdnsRoot';

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
/** A copy button that says whether it worked.
 *
 * `CopyButton`'s `copied` flag is the whole point: a click with no
 * feedback is indistinguishable from a click that missed, and on a value
 * shown exactly once that ambiguity is expensive — you cannot check by
 * pasting somewhere and coming back, because coming back is not
 * available. The tick is the acknowledgement. */
function CopyValue({ value, what }: { value: string; what: string }) {
  return (
    <CopyButton value={value} timeout={2000}>
      {({ copied, copy }) => (
        <Tooltip label={copied ? 'Copied' : `Copy the ${what}`} withArrow>
          <ActionIcon
            variant="subtle"
            color={copied ? 'teal' : 'gray'}
            onClick={copy}
            aria-label={`Copy the ${what}`}
            data-testid={`copy-${what}`}
          >
            {copied ? <IconCheck size={16} /> : <IconCopy size={16} />}
          </ActionIcon>
        </Tooltip>
      )}
    </CopyButton>
  );
}

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
        {/* Both values get a copy button, and the secret is the reason.
            It is a long opaque run that has to be transcribed *exactly*
            into a router, is shown once, and cannot be recovered — so a
            selection that clips a character produces a device that
            authenticates today and fails whenever the router next
            re-reads its config. Selecting by hand is the step most likely
            to go wrong at the one moment that cannot be repeated.

            The username gets one too, because it is copied in the same
            sitting into the same form, and offering the button on only
            one of the pair invites hand-selecting the other. */}
        <Stack gap="xs">
          <Group gap="sm" align="center" wrap="nowrap">
            <span className="ddns-th" style={{ minWidth: '5rem' }}>
              Username
            </span>
            <span className="ddns-data" data-testid="issued-username">
              {issued.device.username}
            </span>
            <CopyValue value={issued.device.username} what="username" />
          </Group>
          <Group gap="sm" align="center" wrap="nowrap">
            <span className="ddns-th" style={{ minWidth: '5rem' }}>
              Secret
            </span>
            <Code
              className="ddns-data"
              data-testid="issued-secret"
              style={{ wordBreak: 'break-all' }}
            >
              {issued.secret}
            </Code>
            <CopyValue value={issued.secret} what="secret" />
          </Group>
        </Stack>
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

/** `SecretOnce`, in the modal it is always shown in.
 *
 * Both places that issue a credential — creating a device, and rotating an
 * existing one — show the same thing and must show it the same way. They did
 * not: creation opened a modal, rotation printed the secret **inline inside
 * the device card**, where it competes with the form around it and scrolls
 * away like ordinary content. A credential shown once should be the only
 * thing on screen while it is on screen.
 *
 * `zIndex` is explicit, and that is the whole reason this is one component
 * rather than two similar ones. Mantine gives every modal the same z-index,
 * so siblings stack by mount order — fine until a re-render changes the
 * order and the secret ends up behind the form that produced it. Rotation
 * makes that concrete: the card is itself a modal, so the secret has to
 * outrank a modal that is already open, not merely appear.
 */
export function SecretOnceModal({
  issued,
  onDismiss,
  title = 'Device created',
}: {
  /** `null` closes it. The caller holds the secret in component state and
   *  nowhere else — not the query cache, not storage, not a ref that
   *  survives a remount. */
  issued: DeviceSecret | null;
  onDismiss: () => void;
  /** What produced the credential. The two callers say different things,
   *  and "Device created" over a rotation would be wrong about the one
   *  fact the reader needs. */
  title?: string;
}) {
  return (
    <Modal
      opened={issued !== null}
      onClose={onDismiss}
      title={title}
      /* Wide enough for the secret to sit on one line. At the default
         width it wrapped mid-token, which makes a value that must be
         transcribed exactly look like two values — and hand-selection
         across a wrap is where a character goes missing. */
      size="lg"
      zIndex={400}
      data-testid="device-secret-modal"
    >
      {/* Portalled to `document.body`, outside `[data-ddns-root]`, so
          without this the secret renders with none of `ddns.css` — and
          `.ddns-data` is what makes it selectable as one monospaced run
          rather than reflowing prose. */}
      <DdnsPortalScope>
        {issued ? <SecretOnce issued={issued} onDismiss={onDismiss} /> : null}
      </DdnsPortalScope>
    </Modal>
  );
}
