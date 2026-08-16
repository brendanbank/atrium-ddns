/** The credential form — the surface the blank-preserves rule protects.
 *
 * ## Why there is a mode selector and not just three text boxes
 *
 * A credential form's hard problem is not entering a secret. It is
 * **saying nothing about one**. A user opening this form to change a
 * TTL has no opinion about the stored API key, and an empty text box
 * cannot express that: *I did not touch this* and *I want this gone*
 * render identically, and the form has to pick one on their behalf.
 *
 * Picking "replace with what is in the box" silently blanks a working
 * provider credential, and the failure surfaces days later as a DNS
 * update failing with nothing pointing at the cause. Picking "ignore
 * empty boxes" makes a credential settable and never unsettable, which
 * is the other half of the same defect and is why
 * `apply_secret_update` has a clear path at all.
 *
 * So the form asks. Three radio options, one per wire value, and
 * `buildCredentialsPayload` is the only thing that turns them into a
 * payload — so `""` is produced by *keep* and by nothing else, and
 * never by a box being empty. `api/credentials.ts` is the contract and
 * `src/test/credentials.test.ts` asserts all three directions on it.
 *
 * ## Why the fields come from the server
 *
 * `credential_keys` is `BaseProvider.REQUIRED_CREDENTIALS`, shipped by
 * `GET /providers`. A field list typed into this file would be the
 * identical defect one release later — a provider gains a key, the form
 * keeps rendering the old two, and every credential it writes is
 * incomplete in a way the server can only refuse or the provider can
 * only fail on.
 *
 * ## Why #88 extended this rather than forking it
 *
 * The create-zone flow needs the same fields with a zone name above
 * them and a different submit label. Everything above is the reason a
 * second copy would be a defect and not a duplication: the mode
 * selector, `buildCredentialsPayload`, and the server-derived field
 * list are one implementation of one rule, and the create path is
 * precisely where a fork would look harmless — a new zone has nothing
 * stored, so two of the three modes are unreachable and the copy would
 * pass its own tests on the day it was written.
 *
 * So the form grew three optional slots — `header`, `footer`,
 * `submitLabel` — and no new behaviour. A caller composing around it
 * cannot change what leaves the browser.
 */
import type { ReactNode } from 'react';
import { useState } from 'react';
import {
  Alert,
  Button,
  Group,
  PasswordInput,
  Radio,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';

import {
  CredentialFormError,
  buildCredentialsPayload,
  defaultCredentialMode,
  type CredentialMode,
  type CredentialsPayload,
} from '../api/credentials';
import type { DomainBackend, Provider } from '../api/domains';

export interface BackendFormValue {
  backend_type: string;
  config: Record<string, unknown>;
  credentials: CredentialsPayload;
}

/** The three options, with the sentence each one is committing to.
 *
 * Written out rather than labelled `Keep / Replace / Clear`, because
 * the consequence is the part a user needs and the verb is the part
 * they already have.
 */
const MODE_LABELS: Record<CredentialMode, string> = {
  keep: 'Keep the stored credential (leave it exactly as it is)',
  replace: 'Replace it with the values below',
  clear: 'Remove the stored credential — this backend will stop working',
};

function parseConfig(text: string): Record<string, unknown> | null {
  if (text.trim() === '') return {};
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function BackendForm({
  providers,
  existing,
  onSubmit,
  onCancel,
  busy,
  header,
  footer,
  submitLabel = 'Save',
  submitDisabled = false,
}: {
  providers: Provider[];
  /** `undefined` for a new binding. When present, the form is editing —
   *  and `credentials_set` is what decides the safe default mode. */
  existing?: DomainBackend;
  onSubmit: (value: BackendFormValue) => void;
  /** Omitted renders no Cancel button. #88's create-zone modal draws the
   *  escape hatch as "add a provider later" instead — a different act
   *  with a different consequence — and a Cancel beside it would offer
   *  two exits that mean different things and look the same. */
  onCancel?: () => void;
  busy: boolean;
  /** Rendered above the provider select. #88 puts the zone name here so
   *  the zone and its first provider are one submission (design §10.1).
   *  Deliberately a slot and not a `zoneName` prop: this form knows
   *  about providers and credentials, and giving it a second subject
   *  would make it the create-zone form rather than a reusable one. */
  header?: ReactNode;
  /** Rendered at the left of the button row — the "add a provider
   *  later" link. */
  footer?: ReactNode;
  submitLabel?: string;
  /** Additional reason the submit is unavailable, ANDed with the form's
   *  own. The caller's condition cannot make the button *available*
   *  when the form's own checks say otherwise. */
  submitDisabled?: boolean;
}) {
  const [service, setService] = useState(
    existing?.backend_type ?? providers[0]?.service ?? '',
  );
  const [mode, setMode] = useState<CredentialMode>(
    defaultCredentialMode(existing?.credentials_set ?? false),
  );
  const [values, setValues] = useState<Record<string, string>>({});
  const [configText, setConfigText] = useState(
    JSON.stringify(existing?.config ?? {}, null, 2),
  );
  const [problem, setProblem] = useState<string | null>(null);

  const keys =
    providers.find((provider) => provider.service === service)
      ?.credential_keys ?? [];

  const submit = () => {
    const config = parseConfig(configText);
    if (config === null) {
      setProblem('Settings must be a JSON object, for example {"ttl": 60}.');
      return;
    }
    let credentials: CredentialsPayload;
    try {
      credentials = buildCredentialsPayload(mode, keys, values);
    } catch (error) {
      if (error instanceof CredentialFormError) {
        // Names the fields, never their values: an error message
        // reaches the DOM, and a credential in the DOM is a credential
        // in a screenshot.
        setProblem(error.message);
        return;
      }
      throw error;
    }
    setProblem(null);
    onSubmit({ backend_type: service, config, credentials });
  };

  return (
    <Stack gap="sm">
      {problem ? (
        <Alert color="gray" variant="light" data-testid="backend-form-problem">
          <Text size="sm">{problem}</Text>
        </Alert>
      ) : null}

      {header}

      <Select
        label="Provider"
        data={providers.map((provider) => provider.service)}
        value={service}
        // Editing a binding cannot change its provider: the row is
        // UNIQUE(domain_id, backend_type), so a change is a different
        // row, and doing it silently would leave the old one behind.
        disabled={existing !== undefined}
        onChange={(value) => {
          setService(value ?? '');
          setValues({});
        }}
        data-testid="backend-service"
      />

      <TextInput
        label="Settings (JSON)"
        description="Non-secret provider settings — hosted-zone id, TTL, nameserver. Never a credential: this column is stored in plaintext and the API refuses one."
        value={configText}
        onChange={(event) => setConfigText(event.currentTarget.value)}
        data-testid="backend-config"
      />

      {existing?.credentials_set ? (
        <Text size="xs" c="dimmed" data-testid="credential-stored">
          A credential is stored for this backend. It cannot be displayed — it
          is encrypted and the interface never asks for it back.
        </Text>
      ) : null}

      <Radio.Group
        label="Credential"
        value={mode}
        onChange={(value) => setMode(value as CredentialMode)}
        data-testid="credential-mode"
      >
        <Stack gap={4} mt="xs">
          {(['keep', 'replace', 'clear'] as const).map((option) => (
            <Radio
              key={option}
              value={option}
              label={MODE_LABELS[option]}
              // `keep` and `clear` are meaningless with nothing stored:
              // there is nothing to keep and nothing to remove. Offering
              // them would make the form able to describe a state the
              // row cannot be in.
              disabled={
                option !== 'replace' && !(existing?.credentials_set ?? false)
              }
              data-testid={`credential-mode-${option}`}
            />
          ))}
        </Stack>
      </Radio.Group>

      {mode === 'replace' ? (
        <Stack gap="xs">
          {keys.length === 0 ? (
            <Text size="sm" data-testid="no-credential-fields">
              This build ships no adapter for {service}, so it declares no
              credential fields.
            </Text>
          ) : null}
          {keys.map((key) => (
            <PasswordInput
              key={key}
              label={key}
              value={values[key] ?? ''}
              onChange={(event) => {
                // Read *before* entering the updater. A functional
                // `setState` runs during the next render, by which point
                // React has detached the synthetic event and
                // `event.currentTarget` is `null` — so the lazy spelling
                // throws `Cannot read properties of null` on the second
                // keystroke rather than on the first, which is why it
                // survives a casual manual test.
                const value = event.currentTarget.value;
                setValues((current) => ({ ...current, [key]: value }));
              }}
              data-testid={`credential-${key}`}
            />
          ))}
        </Stack>
      ) : null}

      <Group justify={footer ? 'space-between' : 'flex-end'} align="center">
        {footer}
        <Group gap="xs">
          {onCancel ? (
            <Button
              size="xs"
              variant="default"
              onClick={onCancel}
              disabled={busy}
            >
              Cancel
            </Button>
          ) : null}
          <Button
            size="xs"
            onClick={submit}
            disabled={busy || service === '' || submitDisabled}
            data-testid="backend-submit"
          >
            {submitLabel}
          </Button>
        </Group>
      </Group>
    </Stack>
  );
}
