/** The zone modal — one component, one provider, driven by the URL.
 *
 * This is a rewrite rather than an edit, because the shape it replaced
 * was wrong in four ways at once and each patch made the next one worse.
 * What the operator reported, and what each one turned out to mean:
 *
 * 1. **"Everything is doubled up."** The card rendered one editing block
 *    per binding, and a fixture zone with two rendered two identical
 *    stacks. The real answer is not a heading per block: **a zone has one
 *    provider.** Split-horizon DNS is two zones with different providers,
 *    not one zone with two. The schema still permits N — `/nic/update`
 *    aggregates across `Domain.backends` and the frozen wire table has
 *    cases for it — so this surface edits the first and says so, rather
 *    than pretending the others cannot exist.
 * 2. **"Reload should keep the modal up."** It could not: the open zone
 *    lived in `useState` in the list. It lives in the pathname now, so
 *    `/atrium-ddns/zones/7` renders the list with this modal over it,
 *    survives a refresh, and can be pasted into a ticket.
 * 3. **"Names in this zone is for another interface."** Gone. The list
 *    row links to the names surface filtered to the zone instead, which
 *    is one line on the row rather than a table inside a form.
 * 4. **Two buttons that both said Save, and one that said Cancel and
 *    deleted.** One button row, conditional on whether the zone exists.
 *
 * ## TTL is a number, and Settings is where the rest goes
 *
 * TTL lives at `ddns_domain_backend.config['ttl']` — #74 made it the
 * middle of three levels (per-hostname override, then this, then
 * `DEFAULT_TTL`). It was only reachable by typing JSON, which is a
 * numeric field with a syntax error for a UI. It is a `NumberInput` now,
 * and `splitTtl`/`mergeTtl` are the *only* two functions that move it
 * between the field and the config object — so the JSON box never shows
 * `ttl` twice and never silently drops it.
 */
import { useState } from 'react';
import {
  Alert,
  Button,
  Collapse,
  Group,
  Modal,
  NumberInput,
  Radio,
  Select,
  Stack,
  Text,
  Textarea,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import {
  DOMAINS_QUERY_KEY,
  DOMAIN_PERMISSION,
  createBackend,
  createDomain,
  deleteDomain,
  domainsQuery,
  providersQuery,
  renameDomain,
  updateBackend,
  type Domain,
  type DomainBackend,
} from '../api/domains';
import {
  CredentialFormError,
  buildCredentialsPayload,
  defaultCredentialMode,
  type CredentialMode,
} from '../api/credentials';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { CARD_MODAL_PADDING_PX, CARD_MODAL_WIDTH_PX } from '../cards';

/** The three credential options, each stating its consequence rather
 *  than its verb. Lifted verbatim from the form this replaces — the
 *  wording is the part that was right. */
const MODE_LABELS: Record<CredentialMode, string> = {
  keep: 'Keep the stored credential (leave it exactly as it is)',
  replace: 'Replace it with the values below',
  clear: 'Remove the stored credential — this zone will stop publishing',
};

/** Config minus its TTL, and the TTL. One function, so the box and the
 *  field can never both claim the same key. */
export function splitTtl(config: Record<string, unknown> | undefined): {
  ttl: number | null;
  rest: Record<string, unknown>;
} {
  const { ttl, ...rest } = config ?? {};
  return { ttl: typeof ttl === 'number' ? ttl : null, rest };
}

/** The inverse. A `null` TTL omits the key entirely rather than writing
 *  `null` — the server reads a missing `ttl` as *inherit*, and `null` is
 *  a value it would have to interpret. */
/** The TTL a new binding starts with. 60s is short enough that a
 *  fixed address moving is visible within the minute, which is what a
 *  dynamic-DNS record is for — a record whose whole purpose is to change
 *  should not be cached for an hour. Existing bindings keep whatever
 *  they have; this is a default, not a migration. */
export const DEFAULT_TTL_SECONDS = 60;

export function mergeTtl(
  rest: Record<string, unknown>,
  ttl: number | null,
): Record<string, unknown> {
  return ttl === null ? rest : { ...rest, ttl };
}

function parseJsonObject(text: string): Record<string, unknown> | null {
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

/** A disclosure triangle, because Mantine has no one-line version and a
 *  `<details>` element cannot be styled to match the rest of the form.
 *  Rotated with a transform so the glyph is one character in both
 *  states and does not reflow the row when it turns. */
function Disclosure({
  open,
  label,
  onToggle,
  testId,
}: {
  open: boolean;
  label: string;
  onToggle: () => void;
  testId: string;
}) {
  return (
    <UnstyledButton
      onClick={onToggle}
      aria-expanded={open}
      data-testid={testId}
      style={{ display: 'flex', alignItems: 'center', gap: 6 }}
    >
      <span
        aria-hidden="true"
        style={{
          display: 'inline-block',
          transition: 'transform 120ms ease',
          transform: open ? 'rotate(90deg)' : 'none',
          fontSize: 10,
          lineHeight: 1,
        }}
      >
        ▶
      </span>
      <Text size="sm" fw={500}>
        {label}
      </Text>
    </UnstyledButton>
  );
}

export interface ZoneModalProps {
  /** `null` is **create**. Not *closed* — `opened` says that. One value
   *  cannot carry both meanings, and trying to make it was how the
   *  create form and the edit form drifted into two components. */
  zoneId: number | null;
  opened: boolean;
  /** Called for the close button, the escape key, and a successful
   *  save. The caller navigates; this component never touches the URL,
   *  so there is one place that decides where closing goes. */
  onClose: () => void;
}

export function ZoneModal({ zoneId, opened, onClose }: ZoneModalProps) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={zoneId === null ? 'Add a zone' : 'Zone'}
      size={CARD_MODAL_WIDTH_PX}
      padding={CARD_MODAL_PADDING_PX}
      data-testid="zone-modal"
    >
      {/* Portalled to `document.body`, outside `[data-ddns-root]`, so
          without this the whole form renders with none of `ddns.css`. */}
      <DdnsPortalScope>
        {opened ? (
          // Keyed so switching zones without closing rebuilds the form
          // state. Without it the previous zone's TTL and credential
          // mode survive into the next one.
          <ZoneModalBody key={zoneId ?? 'new'} zoneId={zoneId} onClose={onClose} />
        ) : null}
      </DdnsPortalScope>
    </Modal>
  );
}

function ZoneModalBody({
  zoneId,
  onClose,
}: {
  zoneId: number | null;
  onClose: () => void;
}) {
  const hasPerm = usePerm();
  const canRead = hasPerm(DOMAIN_PERMISSION);
  const client = useQueryClient();
  const domains = useQuery(domainsQuery({ enabled: canRead }));
  const providers = useQuery(providersQuery({ enabled: canRead }));

  const creating = zoneId === null;
  const domain: Domain | undefined = creating
    ? undefined
    : domains.data?.find((d) => d.id === zoneId);
  /** The binding this form edits. A zone has one provider; if a legacy
   *  row carries more, the extras are named below rather than hidden. */
  const binding: DomainBackend | undefined = domain?.backends[0];
  const extras = domain ? domain.backends.length - 1 : 0;

  const initial = splitTtl(binding?.config);
  /** Seeded once, from the row. Fields the provider describes are lifted
   *  out of the JSON box below so a value never appears in two editable
   *  places — the bug the TTL field would otherwise reintroduce. */
  const [seeded, setSeeded] = useState(false);
  const [name, setName] = useState(domain?.name ?? '');
  const [service, setService] = useState(binding?.backend_type ?? '');
  const [ttl, setTtl] = useState<number | null>(
    binding ? initial.ttl : DEFAULT_TTL_SECONDS,
  );
  const [settingsText, setSettingsText] = useState(
    Object.keys(initial.rest).length === 0
      ? ''
      : JSON.stringify(initial.rest, null, 2),
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  /** Values for the fields the provider describes. Seeded from the
   *  stored config; a field with a `default` prefills it on a new
   *  binding. */
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<CredentialMode>(
    defaultCredentialMode(binding?.credentials_set ?? false),
  );
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const invalidate = () =>
    client.invalidateQueries({ queryKey: DOMAINS_QUERY_KEY });
  const fail = (err: Error) => setError(err.message);
  /** Every successful mutation ends the same way: refresh the list the
   *  modal sits on, then close. The operator asked for exactly this on
   *  delete, and there is no reason save should behave differently. */
  const finish = () => {
    setError(null);
    void invalidate();
    onClose();
  };

  const addZone = useMutation({ mutationFn: createDomain, onSuccess: finish, onError: fail });
  const addBinding = useMutation({
    mutationFn: (input: Parameters<typeof createBackend>) =>
      createBackend(input[0], input[1]),
    onSuccess: finish,
    onError: fail,
  });
  const editBinding = useMutation({
    mutationFn: (input: { id: number; config: Record<string, unknown>; credentials: ReturnType<typeof buildCredentialsPayload> }) =>
      updateBackend(input.id, { config: input.config, credentials: input.credentials }),
    onSuccess: finish,
    onError: fail,
  });
  const rename = useMutation({
    mutationFn: (input: { id: number; name: string }) =>
      renameDomain(input.id, input.name),
    onError: fail,
  });
  const dropZone = useMutation({
    mutationFn: deleteDomain,
    onSuccess: finish,
    onError: fail,
  });

  const busy =
    addZone.isPending ||
    addBinding.isPending ||
    editBinding.isPending ||
    rename.isPending ||
    dropZone.isPending;

  const offered = (providers.data ?? []).map((p) => p.service);
  /** The stored provider is always selectable, even when the catalogue
   *  has stopped offering it. `known_services()` withholds the scripted
   *  compat backends from `/providers` on purpose, and without this a
   *  fixture zone's Provider box rendered **blank** — which reads as
   *  "there are no providers" when there are three. */
  const options =
    service && !offered.includes(service) ? [service, ...offered] : offered;
  const chosen = (providers.data ?? []).find((p) => p.service === service);
  const credentialKeys = chosen?.credential_keys ?? [];
  /** The provider's non-secret settings, as the server describes them.
   *  Anything the provider does not describe stays in the JSON box, so
   *  a setting nobody has modelled is still reachable. */
  const settingFields = chosen?.setting_fields ?? [];
  const described = new Set(settingFields.map((f) => f.key));

  if (!seeded && chosen) {
    setSeeded(true);
    const seed: Record<string, string> = {};
    for (const f of settingFields) {
      const stored = initial.rest[f.key];
      seed[f.key] =
        typeof stored === 'string' ? stored : binding ? '' : f.default;
    }
    setFieldValues(seed);
    // Whatever the provider does not describe stays in the JSON box.
    const leftovers = Object.fromEntries(
      Object.entries(initial.rest).filter(([k]) => !described.has(k)),
    );
    setSettingsText(
      Object.keys(leftovers).length === 0
        ? ''
        : JSON.stringify(leftovers, null, 2),
    );
  }

  if (!canRead) {
    return (
      <Alert color="gray" variant="light" title="Not available to this account" data-testid="zone-refused">
        <Text size="sm">
          Managing zones needs the <code>{DOMAIN_PERMISSION}</code> permission.
          This is a refusal, not an empty zone.
        </Text>
      </Alert>
    );
  }
  if (domains.isLoading || providers.isLoading) {
    return <Text size="sm" data-testid="zone-loading">Loading…</Text>;
  }
  if (!creating && !domain) {
    // "You own no zones" and "zone N is not one of yours" are different
    // sentences, and a shared empty state claims something untrue.
    return (
      <Alert color="gray" variant="light" title="No such zone" data-testid="zone-missing">
        <Text size="sm">
          Zone <code>{zoneId}</code> is not one of yours, or it has been deleted.
        </Text>
      </Alert>
    );
  }

  const submit = () => {
    const rest = parseJsonObject(settingsText);
    if (rest === null) {
      setError('Settings must be a JSON object, for example {"hosted_zone_id": "Z123"}.');
      return;
    }
    if ('ttl' in rest) {
      // Otherwise the field and the box both write `ttl` and the last
      // one wins silently. Naming it is cheaper than picking.
      setError('Remove "ttl" from Settings — it has its own field above.');
      return;
    }
    for (const f of settingFields) {
      const value = (fieldValues[f.key] ?? '').trim();
      if (f.required && value === '') {
        // Refused here rather than at publish time. Without all of
        // these `has_credentials()` is false and every update under the
        // zone answers 911 — a failure that surfaces days later with
        // nothing pointing at the cause.
        setError(`${f.label} is required for ${service}.`);
        return;
      }
      if (value !== '') rest[f.key] = value;
    }
    const config = mergeTtl(rest, ttl);
    let credentials;
    try {
      credentials = buildCredentialsPayload(mode, credentialKeys, secrets);
    } catch (err) {
      if (err instanceof CredentialFormError) {
        // Names the fields, never their values: this string reaches the
        // DOM, and a credential in the DOM is a credential in a
        // screenshot.
        setError(err.message);
        return;
      }
      throw err;
    }
    setError(null);

    if (!domain) {
      addZone.mutate({ name: name.trim(), backend: { backend_type: service, config, credentials } });
      return;
    }
    const saveBinding = () => {
      if (binding) {
        editBinding.mutate({ id: binding.id, config, credentials });
        return;
      }
      addBinding.mutate([domain.id, { backend_type: service, config, credentials }]);
    };
    if (name.trim() !== '' && name.trim() !== domain.name) {
      // Rename first; only touch the binding if it succeeded. The other
      // order writes a credential against a name the server then refuses
      // to change — a form that saved half of what you typed and said so
      // about neither half.
      rename.mutate({ id: domain.id, name: name.trim() }, { onSuccess: saveBinding });
      return;
    }
    saveBinding();
  };

  return (
    <Stack gap="md" data-testid="zone-modal-body">
      {error ? (
        <Alert color="gray" variant="light" title="That did not work" data-testid="zone-modal-error">
          <Text size="sm" ff="monospace">{error}</Text>
        </Alert>
      ) : null}

      <Stack gap={4}>
        <Text size="sm" fw={500}>Zone</Text>
        <input
          className="ddns-data"
          aria-label="Zone name"
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          data-testid="zone-name"
        />
        <Text size="xs" c="dimmed">
          Stored lower-cased and without a trailing dot. Zone names are unique
          across the whole installation — DNS is global.
          {domain && domain.hostname_count > 0
            ? ` This zone has ${domain.hostname_count} name${domain.hostname_count === 1 ? '' : 's'} under it; a rename that would leave any of them outside the zone is refused rather than rewriting them.`
            : ''}
        </Text>
      </Stack>

      <Select
        label="Provider"
        description="One provider per zone. For split-horizon, add a second zone with the same name is not possible — use separate zones with their own providers."
        data={options}
        value={service || null}
        // A binding's provider cannot change: the row is
        // UNIQUE(domain_id, backend_type), so a change is a different
        // row and doing it silently would leave the old one behind.
        disabled={binding !== undefined}
        onChange={(value) => {
          setService(value ?? '');
          setSecrets({});
        }}
        data-testid="zone-provider"
      />

      {/* The provider's own settings, described by the server. A select
          where the value is an enum: an algorithm the nameserver will
          not accept is a publish-time failure from a typo, and a fixed
          list cannot make that typo. */}
      {settingFields.map((f) =>
        f.choices.length > 0 ? (
          <Select
            key={f.key}
            label={f.label}
            description={f.help}
            data={f.choices}
            value={fieldValues[f.key] || null}
            withAsterisk={f.required}
            onChange={(value) =>
              setFieldValues((c) => ({ ...c, [f.key]: value ?? '' }))
            }
            data-testid={`zone-setting-${f.key}`}
          />
        ) : (
          <TextInput
            key={f.key}
            label={f.label}
            description={f.help}
            value={fieldValues[f.key] ?? ''}
            withAsterisk={f.required}
            onChange={(event) =>
              setFieldValues((c) => ({ ...c, [f.key]: event.currentTarget.value }))
            }
            data-testid={`zone-setting-${f.key}`}
          />
        ),
      )}

      <NumberInput
        label="TTL (seconds)"
        description="How long resolvers may cache this zone's records. Blank inherits the installation default."
        value={ttl ?? ''}
        min={1}
        allowDecimal={false}
        onChange={(value) =>
          setTtl(value === '' || value === null ? null : Number(value))
        }
        data-testid="zone-ttl"
      />

      <Stack gap={4}>
        <Disclosure
          open={settingsOpen}
          onToggle={() => setSettingsOpen((v) => !v)}
          label="Settings (JSON)"
          testId="zone-settings-toggle"
        />
        <Collapse expanded={settingsOpen}>
          <Stack gap={4}>
            <Text size="xs" c="dimmed">
              Non-secret provider settings — hosted-zone id, nameserver. Never a
              credential: this column is stored in plaintext and the API refuses
              one. TTL has its own field above.
            </Text>
            <Textarea
              autosize
              minRows={3}
              value={settingsText}
              onChange={(event) => setSettingsText(event.currentTarget.value)}
              data-testid="zone-settings"
            />
          </Stack>
        </Collapse>
      </Stack>

      <Stack gap={4}>
        <Text size="sm" fw={500}>Credential</Text>
        {binding?.credentials_set ? (
          <Text size="xs" c="dimmed">
            A credential is stored. It cannot be displayed — it is encrypted and
            the interface never asks for it back.
          </Text>
        ) : null}
        <Radio.Group value={mode} onChange={(value) => setMode(value as CredentialMode)}>
          <Stack gap={4}>
            {(Object.keys(MODE_LABELS) as CredentialMode[])
              // `keep` is meaningless with nothing stored, and offering
              // it would let a zone be created with no credential at all.
              .filter((m) => (binding?.credentials_set ? true : m !== 'keep'))
              .map((m) => (
                <Radio key={m} value={m} label={MODE_LABELS[m]} data-testid={`zone-credential-${m}`} />
              ))}
          </Stack>
        </Radio.Group>
        {mode === 'replace'
          ? credentialKeys.map((key) => (
              <Textarea
                key={key}
                label={key}
                autosize
                minRows={1}
                value={secrets[key] ?? ''}
                onChange={(event) =>
                  setSecrets((current) => ({ ...current, [key]: event.currentTarget.value }))
                }
                data-testid={`zone-credential-field-${key}`}
              />
            ))
          : null}
      </Stack>

      {extras > 0 ? (
        <Alert color="gray" variant="light" data-testid="zone-extra-bindings">
          <Text size="sm">
            This zone has {extras} further provider binding
            {extras === 1 ? '' : 's'} from an earlier import. A zone publishes
            through one provider; the others are still live on the wire and are
            not editable here.
          </Text>
        </Alert>
      ) : null}

      {/* The confirmation replaces the button row rather than opening a
          second modal over the first. A modal on a modal is where the
          "Cancel" that deleted came from — two overlapping dialogs, each
          with its own idea of what the buttons at the bottom mean. */}
      {confirmDelete && domain ? (
        <Alert
          color="gray"
          variant="light"
          title="Delete this zone?"
          data-testid="zone-delete-confirm"
        >
          <Stack gap="sm">
            <Text size="sm">
              This destroys <code className="ddns-data">{domain.name}</code>, its
              provider binding — <strong>stored credentials included</strong> —
              and the {domain.hostname_count} name
              {domain.hostname_count === 1 ? '' : 's'} under it. It does{' '}
              <strong>not</strong> remove whatever your DNS provider has already
              published for those names; those records stay in the zone with
              nothing maintaining them.
            </Text>
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => setConfirmDelete(false)}
                data-testid="zone-delete-cancel"
              >
                Keep it
              </Button>
              <Button
                size="xs"
                color="red"
                disabled={busy}
                onClick={() => dropZone.mutate(domain.id)}
                data-testid="zone-delete-confirmed"
              >
                Delete {domain.name}
              </Button>
            </Group>
          </Stack>
        </Alert>
      ) : null}

      {/* One button row, conditional on whether the zone exists. */}
      <Group justify="space-between">
        {domain ? (
          <Button
            size="xs"
            variant="default"
            color="gray"
            disabled={busy}
            onClick={() => setConfirmDelete(true)}
            data-testid="zone-delete"
          >
            Delete this zone
          </Button>
        ) : (
          <span />
        )}
        <Group gap="sm">
          <Button size="xs" variant="default" disabled={busy} onClick={onClose} data-testid="zone-cancel">
            Cancel
          </Button>
          <Button
            size="xs"
            disabled={busy || name.trim() === '' || service === ''}
            onClick={submit}
            data-testid="zone-submit"
          >
            {domain ? 'Save' : 'Add'}
          </Button>
        </Group>
      </Group>
    </Stack>
  );
}
