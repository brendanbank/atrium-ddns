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
 * ## Why delete asks in a second modal, and what this file used to claim (#159)
 *
 * Deleting a zone destroys the zone, its provider binding **including
 * the stored credential**, and every name under it. It used to ask
 * *inline*, as an `Alert` in this form's own body, with the form's own
 * `Delete this zone` / `Cancel` / `Save` row still live beneath it — so
 * the surface offered *save* and *destroy* at the same moment, with two
 * sets of buttons and two meanings of the word "delete" inches apart.
 * Same defect #153 fixed in `NameModal`, over a much larger blast
 * radius.
 *
 * **This file argued against fixing it, and the argument was false
 * about its own code.** The comment above the confirmation read: *"The
 * confirmation replaces the button row rather than opening a second
 * modal over the first."* It did not. The `<Group>` carrying `Delete
 * this zone` / `Cancel` / `Save` was rendered unconditionally,
 * immediately after the confirmation's conditional closed. So the file
 * stated a design decision it did not implement, and a reader who
 * trusted it would have left the worst instance of this defect in place
 * believing it had been considered and rejected.
 *
 * **The incident that comment records is real, and it is the reason to
 * lock rather than to stay inline.** *"A modal on a modal is where the
 * `Cancel` that deleted came from"* — that happened. But the hazard is
 * **two live `Cancel`s, not two dialogs**: two overlapping surfaces each
 * with its own idea of what the buttons at the bottom mean. #153's
 * answer removes the ambiguity instead of the second surface:
 *
 * - The confirmation is its own `Modal` at `zIndex={400}` — the number
 *   `SecretOnceModal` uses and `NameModal` and `DeviceCard` borrow, for
 *   the same reason: Mantine gives every modal the same z-index, so
 *   siblings stack by mount order until a re-render changes it.
 * - **The form beneath is locked, not merely covered.** `locked = busy
 *   || confirming` disables every control in this body. Mantine's
 *   overlay stops a *mouse*; it does not stop a keyboard, an assistive
 *   technology or a test, and "cannot be saved while the confirmation
 *   is open" is a statement about the action, not about what is painted
 *   over what.
 * - **The dismissal is spelled `Keep it`, never `Cancel`.** While the
 *   confirmation is up there is no enabled control anywhere on screen
 *   labelled `Cancel`, so no two controls share a word with two
 *   meanings. That is the incident's actual lesson.
 *
 * One more consequence: **the delete error renders inside the
 * confirmation.** This form's own error `Alert` is at the top of the
 * body, behind the dialog — a failed delete would have put the server's
 * words on a surface nobody was looking at. `dropZone` therefore does
 * not use `onError: fail`.
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
import { CARD_MODAL_PADDING_PX, CARD_MODAL_WIDTH_PX, CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';

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
  disabled,
}: {
  open: boolean;
  label: string;
  onToggle: () => void;
  testId: string;
  /** Part of the form's lock, not a state of its own — see `locked` in
   *  `ZoneModalBody`. `UnstyledButton` renders a real `<button>`, so
   *  this is the DOM's own `disabled` and not a painted-over one. */
  disabled?: boolean;
}) {
  return (
    <UnstyledButton
      onClick={onToggle}
      aria-expanded={open}
      disabled={disabled}
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
      {...CARD_MODAL_PROPS}
      styles={CARD_MODAL_STYLES}
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
  /** **Nothing here may be seeded by a `useState` initialiser.**
   *
   *  On a reload of `?zone=3091` the queries have not resolved on the
   *  first render, so `domain` is `undefined` — and a `useState`
   *  initialiser runs exactly once. Every field seeded that way stayed
   *  empty for the life of the modal, no matter what arrived a moment
   *  later. The zone name box came up blank on a zone that plainly had
   *  a name.
   *
   *  So the fields start empty and are filled by the render-phase seed
   *  below, once the data is actually there. `seededFrom` records which
   *  zone they were filled from, so reopening on a different zone
   *  re-seeds and reopening on the same one does not stamp on edits in
   *  progress. */
  const [seededFrom, setSeededFrom] = useState<number | null | undefined>(
    undefined,
  );
  const [name, setName] = useState('');
  const [service, setService] = useState('');
  const [ttl, setTtl] = useState<number | null>(null);
  const [settingsText, setSettingsText] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  /** Values for the fields the provider describes. Seeded from the
   *  stored config; a field with a `default` prefills it on a new
   *  binding. */
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<CredentialMode>('replace');
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
    // Deliberately **not** `onError: fail`. `fail` writes this form's
    // own error `Alert`, which lives at the top of the body — behind
    // the confirmation dialog. A failed delete would have put the
    // server's words on a surface nobody was looking at. It is rendered
    // inside the dialog instead, from `dropZone.error`.
  });

  const busy =
    addZone.isPending ||
    addBinding.isPending ||
    editBinding.isPending ||
    rename.isPending ||
    dropZone.isPending;

  /** The confirmation is open, and there is a zone for it to be about.
   *  `domain` is in the condition because the dialog names the zone and
   *  counts the names under it. */
  const confirming = confirmDelete && domain != null;

  /** What every control in this body is disabled by.
   *
   *  Not decoration, and not the overlay's job. Mantine's overlay stops
   *  a mouse; it does not stop a keyboard, an assistive technology or a
   *  test. "This form cannot be saved while the delete confirmation is
   *  open" is a statement about the action. See the docblock. */
  const locked = busy || confirming;

  const offered = (providers.data ?? []).map((p) => p.service);
  /** The stored provider is always selectable, even when the catalogue
   *  has stopped offering it. `known_services()` withholds the scripted
   *  compat backends from `/providers` on purpose, and without this a
   *  fixture zone's Provider box rendered **blank** — which reads as
   *  "there are no providers" when there are three. */
  /* The seed. A render-phase update — React re-runs this render with the
     new state before touching the DOM, which is the documented way to
     derive state from data that arrives late.

     Keyed on the zone identity rather than a boolean: `undefined` means
     "not seeded yet", and a zone id (or `null` for create) means "seeded
     from that one". A boolean could not tell reopening-on-another-zone
     from a re-render. */
  const ready = !domains.isLoading && !providers.isLoading;
  const identity = domain ? domain.id : creating ? null : undefined;
  if (ready && seededFrom !== identity && identity !== undefined) {
    setSeededFrom(identity);
    const svc = binding?.backend_type ?? '';
    const fields =
      (providers.data ?? []).find((p) => p.service === svc)?.setting_fields ??
      [];
    const known = new Set(fields.map((f) => f.key));
    setName(domain?.name ?? '');
    setService(svc);
    setTtl(binding ? initial.ttl : DEFAULT_TTL_SECONDS);
    setMode(defaultCredentialMode(binding?.credentials_set ?? false));
    setFieldValues(
      Object.fromEntries(
        fields.map((f) => {
          const stored = initial.rest[f.key];
          return [
            f.key,
            typeof stored === 'string' ? stored : binding ? '' : f.default,
          ];
        }),
      ),
    );
    // Whatever the provider does not describe stays in the JSON box.
    const leftovers = Object.fromEntries(
      Object.entries(initial.rest).filter(([k]) => !known.has(k)),
    );
    setSettingsText(
      Object.keys(leftovers).length === 0
        ? ''
        : JSON.stringify(leftovers, null, 2),
    );
  }

  const options =
    service && !offered.includes(service) ? [service, ...offered] : offered;
  const chosen = (providers.data ?? []).find((p) => p.service === service);
  const credentialKeys = chosen?.credential_keys ?? [];
  /** The provider's non-secret settings, as the server describes them.
   *  Anything the provider does not describe stays in the JSON box, so
   *  a setting nobody has modelled is still reachable. */
  const settingFields = chosen?.setting_fields ?? [];

  /* When the operator picks a different provider, its described fields
     change and their defaults have to be re-seeded. Keyed on the service
     so switching back and forth does not wipe what was typed for the one
     already selected. */
  const [fieldsFor, setFieldsFor] = useState<string | null>(null);
  if (chosen && fieldsFor !== service) {
    setFieldsFor(service);
    setFieldValues((current) =>
      Object.fromEntries(
        settingFields.map((f) => {
          const stored = initial.rest[f.key];
          return [
            f.key,
            current[f.key] ??
              (typeof stored === 'string' ? stored : binding ? '' : f.default),
          ];
        }),
      ),
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
          disabled={locked}
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
        data={options}
        value={service || null}
        // A binding's provider cannot change: the row is
        // UNIQUE(domain_id, backend_type), so a change is a different
        // row and doing it silently would leave the old one behind.
        // `locked` is the second reason, and either one is sufficient.
        disabled={binding !== undefined || locked}
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
            data={f.choices}
            value={fieldValues[f.key] || null}
            withAsterisk={f.required}
            disabled={locked}
            onChange={(value) =>
              setFieldValues((c) => ({ ...c, [f.key]: value ?? '' }))
            }
            data-testid={`zone-setting-${f.key}`}
          />
        ) : (
          <TextInput
            key={f.key}
            label={f.label}
            value={fieldValues[f.key] ?? ''}
            withAsterisk={f.required}
            disabled={locked}
            onChange={(event) => {
              // Read *before* entering the updater — the same rule
              // `BackendForm` carries, and for the same reason. See
              // the credential field below for the full account.
              const value = event.currentTarget.value;
              setFieldValues((c) => ({ ...c, [f.key]: value }));
            }}
            data-testid={`zone-setting-${f.key}`}
          />
        ),
      )}

      <NumberInput
        label="TTL (seconds)"
        value={ttl ?? ''}
        min={1}
        allowDecimal={false}
        disabled={locked}
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
          disabled={locked}
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
              disabled={locked}
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
                <Radio
                  key={m}
                  value={m}
                  label={MODE_LABELS[m]}
                  // Per-`Radio` rather than on the group: Mantine's
                  // `Radio.Group` does not forward `disabled` to its
                  // children, so a group-level prop would read as a
                  // lock and not be one.
                  disabled={locked}
                  data-testid={`zone-credential-${m}`}
                />
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
                disabled={locked}
                onChange={(event) => {
                  // Read the value *before* entering the updater, and
                  // this is not style. A functional `setState` is only
                  // run eagerly when the fiber has no update already
                  // pending; otherwise React defers it to the render
                  // phase, and by then `executeDispatch`'s `finally`
                  // has set `event.currentTarget` back to `null`. The
                  // lazy spelling therefore throws `Cannot read
                  // properties of null (reading 'value')` *during
                  // render*, which — with no error boundary over the
                  // host tree — makes React unmount the whole host
                  // root. The dialog and the list vanish together and
                  // the `main` landmark is left empty, which reads
                  // like the route swapped rather than like a typing
                  // handler throwing.
                  //
                  // Whether an update is already pending depends on
                  // whether a react-query refetch happened to land in
                  // the same tick, so it is intermittent by nature and
                  // gets *more* likely the busier the machine is. It
                  // was diagnosed once already on `BackendForm`'s
                  // credential field; this form is the copy that was
                  // written from the old one and kept the defect.
                  const value = event.currentTarget.value;
                  setSecrets((current) => ({ ...current, [key]: value }));
                }}
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

      {/* Its own surface, over this one — and this comment is the
          correction of the one that used to stand here. It read: "The
          confirmation replaces the button row rather than opening a
          second modal over the first."

          **It did not.** The `<Group>` below rendered unconditionally,
          immediately after this conditional closed, so `Delete this
          zone` / `Cancel` / `Save` all stayed live behind the panel.
          The file stated a decision it did not implement (#159).

          The incident the old comment records is kept because it is
          real: "a modal on a modal is where the `Cancel` that deleted
          came from — two overlapping dialogs, each with its own idea of
          what the buttons at the bottom mean". That is the reason for
          `locked`, not a reason to stay inline. The hazard is two live
          `Cancel`s, not two dialogs: here every control beneath is
          disabled while this is open, and the dismissal is spelled
          `Keep it`, never `Cancel`.

          `zIndex` is the number `SecretOnceModal`, `NameModal` and
          `DeviceCard` all use, for the identical reason: this has to
          outrank a modal that is already open, and Mantine gives every
          modal the same z-index, so siblings stack by mount order until
          a re-render changes it.

          The testid is on the body rather than on `Modal`, because
          Mantine does not forward `data-testid` to a node a DOM query
          can reach — `deviceCard.test.tsx` records the same. */}
      <Modal
        opened={confirming}
        onClose={() => {
          dropZone.reset();
          setConfirmDelete(false);
        }}
        title="Delete this zone?"
        zIndex={400}
      >
        {/* Portalled to `document.body`, outside `[data-ddns-root]`, so
            without this the dialog renders with none of `ddns.css` —
            and `.ddns-data` is what sets the zone name apart from the
            prose around it. */}
        <DdnsPortalScope>
          <Stack gap="sm" data-testid="zone-delete-confirm">
            {/* Verbatim, and it stays verbatim. It is the only place
                the blast radius is stated: the zone, the stored
                credential, and every name under it — and the records
                the provider has already published, which this does
                *not* remove. */}
            <Text size="sm">
              This destroys{' '}
              <code className="ddns-data">{domain?.name}</code>, its provider
              binding — <strong>stored credentials included</strong> — and the{' '}
              {domain?.hostname_count} name
              {domain?.hostname_count === 1 ? '' : 's'} under it. It does{' '}
              <strong>not</strong> remove whatever your DNS provider has already
              published for those names; those records stay in the zone with
              nothing maintaining them.
            </Text>
            {dropZone.error ? (
              <Alert color="gray" variant="light" data-testid="zone-delete-error">
                <Text size="sm" ff="monospace">
                  {(dropZone.error as Error).message}
                </Text>
              </Alert>
            ) : null}
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => {
                  dropZone.reset();
                  setConfirmDelete(false);
                }}
                // Renamed from `zone-delete-cancel`, which named the
                // one word this dialog must not use.
                data-testid="zone-delete-keep"
              >
                Keep it
              </Button>
              <Button
                size="xs"
                color="red"
                disabled={busy}
                onClick={() => domain && dropZone.mutate(domain.id)}
                data-testid="zone-delete-confirmed"
              >
                Delete {domain?.name}
              </Button>
            </Group>
          </Stack>
        </DdnsPortalScope>
      </Modal>

      {/* One button row, conditional on whether the zone exists — and
          disabled by `locked`, not merely covered, while the
          confirmation above is open. */}
      <Group justify="space-between">
        {domain ? (
          <Button
            size="xs"
            variant="default"
            color="gray"
            disabled={locked}
            onClick={() => {
              dropZone.reset();
              setConfirmDelete(true);
            }}
            data-testid="zone-delete"
          >
            Delete this zone
          </Button>
        ) : (
          <span />
        )}
        <Group gap="sm">
          <Button size="xs" variant="default" disabled={locked} onClick={onClose} data-testid="zone-cancel">
            Cancel
          </Button>
          <Button
            size="xs"
            disabled={locked || name.trim() === '' || service === ''}
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
