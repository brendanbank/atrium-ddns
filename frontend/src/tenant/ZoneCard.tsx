/** The zone card — one component, two entrances.
 *
 * `docs/ops/ui-design.md` **Part III §17**, which records the operator
 * overruling §12:
 *
 * > **The operator has asked twice for a modal that pops up.** That is
 * > the decision. […] So: click a row → a modal opens. The deep-link
 * > route still resolves, and opens the same card. One card component,
 * > two entrances. Anything else grows a second editor.
 *
 * This file is that one component. `ZoneDetailPage` renders it under
 * `/atrium-ddns/zones/:id` and `DomainList` renders it inside
 * `ZoneCardModal`; neither of them owns any of the editing, and there is
 * no second copy for either to drift from. `src/test/sharedCard.test.tsx`
 * asserts that by module identity — it replaces this module and checks
 * the replacement reaches *both* entrances, which a private copy at
 * either end would fail.
 *
 * ## What §17 does and does not overturn
 *
 * The routes stay. §12's width argument was measured against a 620px
 * `lg` drawer and a 360/800 split and was never an argument against a
 * modal, which takes an arbitrary `size`; the two arguments that
 * survived on their own merits are linkability and Back, and both are
 * properties of a URL. So the row is still an `<a href>` and every
 * modified click still navigates (`cards.ts::opensInThisTab`). What the
 * modal replaces is the *normal* way in, not the address.
 *
 * ## Four states, kept four — five here
 *
 * The table `DomainsPage` and `DeviceBoardPage` keep: refused, loading,
 * failed, empty. This card adds a fifth that is genuinely different and
 * must not collapse into "empty": **the id names a zone this tenant does
 * not have.** "You own no zones" and "zone 7 is not yours" are different
 * sentences, and rendering them as one states a fact about the account
 * that is not true.
 */
import { useState } from 'react';
import {
  Alert,
  Anchor,
  Button,
  Group,
  Modal,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import {
  DOMAINS_QUERY_KEY,
  DOMAIN_PERMISSION,
  createBackend,
  deleteBackend,
  deleteDomain,
  domainsQuery,
  providersQuery,
  renameDomain,
  updateBackend,
  type Domain,
  type DomainBackend,
} from '../api/domains';
import {
  HOSTNAME_PERMISSION,
  hostnamesQuery,
  type Hostname,
} from '../api/hostnames';
import { absoluteTitle, formatAge } from '../board/format';
import { CARD_MODAL_PADDING_PX, CARD_MODAL_WIDTH_PX } from '../cards';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { NAMES_PATH } from '../HostnamesPage';
import { DOMAINS_PATH } from '../paths';
import { BackendForm, type BackendFormValue } from './BackendForm';
import { ZoneNowhereMark, ZoneWireConsequence } from './ZoneStatus';

function BackendLine({
  backend,
  onEdit,
  onDelete,
  busy,
}: {
  backend: DomainBackend;
  onEdit: (backend: DomainBackend) => void;
  onDelete: (backend: DomainBackend) => void;
  busy: boolean;
}) {
  return (
    <Group gap="sm" data-testid={`backend-${backend.backend_type}`}>
      <span className="ddns-data">{backend.backend_type}</span>
      <span className="ddns-label" data-testid={`credentials-${backend.id}`}>
        {/* Three words for three facts, and none of them is the
            credential. "no credential stored" is a working state for a
            binding nobody has finished configuring, and it is not the
            same as one this build cannot resolve at all. */}
        {backend.credentials_set ? 'credential stored' : 'no credential stored'}
      </span>
      {backend.known_service ? null : (
        <span className="ddns-label" data-testid={`unknown-${backend.id}`}>
          no adapter in this build — updates answer 911
        </span>
      )}
      <Button
        size="xs"
        variant="default"
        disabled={busy}
        onClick={() => onEdit(backend)}
        data-testid={`edit-backend-${backend.id}`}
      >
        Edit
      </Button>
      <Button
        size="xs"
        variant="default"
        disabled={busy}
        onClick={() => onDelete(backend)}
        data-testid={`delete-backend-${backend.id}`}
      >
        Remove
      </Button>
    </Group>
  );
}

/** The names under this zone, read-only.
 *
 * §10.2's wireframe says *"the existing name rows, filtered to this
 * zone"*. Read-only on purpose: `HostnameList` owns creating, assigning
 * and deleting a name, and a second surface that also did would be two
 * renderings of one operation. This one answers "what is in here", and
 * links to the surface that answers "change it".
 */
function ZoneNames({ hostnames }: { hostnames: Hostname[] }) {
  if (hostnames.length === 0) {
    return (
      <Text size="sm" data-testid="zone-names-empty">
        No names in this zone yet.
      </Text>
    );
  }
  return (
    <Stack gap="xs">
      <div className="ddns-board__head">
        <span />
        <span className="ddns-label">name</span>
        <span className="ddns-label">last published</span>
        <span className="ddns-label">device</span>
      </div>
      {hostnames.map((hostname) => (
        <div
          key={hostname.id}
          className="ddns-device__line"
          data-testid={`zone-name-${hostname.name}`}
        >
          <span />
          <span className="ddns-data">{hostname.name}</span>
          <span
            className="ddns-station__time"
            title={absoluteTitle(hostname.last_updated_at)}
          >
            {/* `never` for a null, never an epoch-derived age. */}
            {formatAge(hostname.last_updated_at)}
          </span>
          <span className="ddns-station__time">
            {hostname.device_name ?? 'not assigned'}
          </span>
        </div>
      ))}
    </Stack>
  );
}

export interface ZoneCardProps {
  /** The zone this card is about. Parsed from the pathname by the
   *  route, taken from the row by the modal — one number either way, so
   *  the card cannot tell which entrance it came through and does not
   *  need to. */
  zoneId: number;
  /** Called when the zone this card is about has been destroyed.
   *
   * The route's answer is "go back to the list", because staying would
   * render *"no such zone"* for a zone the operator has just deleted on
   * purpose. The modal's answer is "close me", because the list is
   * already underneath. That difference is the *only* thing the two
   * entrances do not share, which is why it is a prop and not a branch
   * on how the card was opened. */
  onDeleted?: () => void;
}

export function ZoneCard({ zoneId, onDeleted }: ZoneCardProps) {
  const hasPerm = usePerm();
  const canRead = hasPerm(DOMAIN_PERMISSION);
  const canReadNames = hasPerm(HOSTNAME_PERMISSION);

  const client = useQueryClient();
  const domains = useQuery(domainsQuery({ enabled: canRead }));
  const providers = useQuery(providersQuery({ enabled: canRead }));
  const hostnames = useQuery(
    hostnamesQuery({ enabled: canRead && canReadNames }),
  );

  const [addingBackend, setAddingBackend] = useState(false);
  const [editing, setEditing] = useState<DomainBackend | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [newZoneName, setNewZoneName] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: DOMAINS_QUERY_KEY });
  };
  const fail = (err: Error) => setError(err.message);
  const done = () => {
    setError(null);
    setAddingBackend(false);
    setEditing(null);
    setRenaming(false);
    setNewZoneName('');
    invalidate();
  };

  const addBackend = useMutation({
    mutationFn: (input: { domainId: number; value: BackendFormValue }) =>
      createBackend(input.domainId, input.value),
    onSuccess: done,
    onError: fail,
  });
  const editBackend = useMutation({
    mutationFn: (input: { id: number; value: BackendFormValue }) =>
      updateBackend(input.id, {
        config: input.value.config,
        credentials: input.value.credentials,
      }),
    onSuccess: done,
    onError: fail,
  });
  const dropBackend = useMutation({
    mutationFn: deleteBackend,
    onSuccess: done,
    onError: fail,
  });
  const renameZone = useMutation({
    mutationFn: (input: { id: number; name: string }) =>
      renameDomain(input.id, input.name),
    onSuccess: done,
    // The server's refusal, verbatim. A rename that would leave names
    // outside the zone is a 409 whose detail carries the count and a
    // sample of the offending names — the whole point of which is that
    // the operator can act on it, so it is not reworded here.
    onError: fail,
  });
  const dropZone = useMutation({
    mutationFn: deleteDomain,
    onSuccess: () => {
      invalidate();
      if (onDeleted) {
        onDeleted();
        return;
      }
      // The default is the route's: the row this card is about no
      // longer exists, and staying here would render "no such zone" for
      // a zone the operator has just deleted on purpose.
      window.location.assign(DOMAINS_PATH);
    },
    onError: fail,
  });

  const busy =
    addBackend.isPending ||
    editBackend.isPending ||
    dropBackend.isPending ||
    renameZone.isPending ||
    dropZone.isPending;

  if (!canRead) {
    return (
      <Alert
        color="gray"
        variant="light"
        title="Not available to this account"
        data-testid="zone-refused"
      >
        <Text size="sm">
          Managing zones needs the <code>{DOMAIN_PERMISSION}</code> permission.
          This is a refusal, not an empty page — ask an administrator for the
          permission rather than assuming this zone does not exist.
        </Text>
      </Alert>
    );
  }

  if (domains.isLoading || providers.isLoading) {
    return (
      <Text size="sm" data-testid="zone-loading">
        Loading…
      </Text>
    );
  }

  if (domains.error || providers.error) {
    return (
      <Alert
        color="gray"
        variant="light"
        title="Could not load this zone"
        data-testid="zone-error"
      >
        <Text size="sm" ff="monospace">
          {((domains.error ?? providers.error) as Error).message}
        </Text>
      </Alert>
    );
  }

  const domain: Domain | undefined = domains.data?.find((d) => d.id === zoneId);
  if (!domain) {
    // The fifth state. "You own no zones" and "zone N is not one of
    // yours" are different sentences; a shared empty state would make a
    // claim about the account that is not true.
    return (
      <Alert
        color="gray"
        variant="light"
        title="No such zone"
        data-testid="zone-missing"
      >
        <Text size="sm">
          Zone <code>{zoneId}</code> is not one of yours, or it has been
          deleted.{' '}
          <Anchor href={DOMAINS_PATH} data-testid="zone-missing-back">
            Back to zones
          </Anchor>
        </Text>
      </Alert>
    );
  }

  const nowhere = domain.backends.length === 0;
  const zoneNames = (hostnames.data ?? []).filter(
    (hostname) => hostname.domain_id === domain.id,
  );

  return (
    <Stack gap="lg" data-testid="zone-card">
      {error ? (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="zone-action-error"
        >
          <Text size="sm" ff="monospace">
            {error}
          </Text>
        </Alert>
      ) : null}

      <div
        className="ddns-zone"
        data-diverged={nowhere ? 'true' : 'false'}
        data-testid={`zone-${domain.name}`}
      >
        <div className="ddns-zone__head">
          <Title order={3} className="ddns-data">
            {domain.name}
          </Title>
          {nowhere ? <ZoneNowhereMark testId="zone-nowhere" /> : null}
          <Button
            size="xs"
            variant="default"
            disabled={busy}
            onClick={() => {
              setRenaming(true);
              setNewZoneName(domain.name);
            }}
            data-testid="zone-rename"
          >
            Rename
          </Button>
          <Button
            size="xs"
            variant="default"
            disabled={busy}
            onClick={() => setConfirmDelete(true)}
            data-testid="zone-delete"
          >
            Delete
          </Button>
        </div>
        <span className="ddns-label">
          {domain.hostname_count} name{domain.hostname_count === 1 ? '' : 's'} ·{' '}
          {domain.backends.length} provider
          {domain.backends.length === 1 ? '' : 's'}
        </span>
        {nowhere ? <ZoneWireConsequence testId="zone-nowhere-why" /> : null}
      </div>

      <Stack gap="xs">
        <span className="ddns-label">publishes through</span>
        {nowhere ? (
          <Text size="sm" data-testid="zone-no-providers">
            Nothing yet. Add a provider and this zone starts publishing.
          </Text>
        ) : (
          domain.backends.map((backend) => (
            <BackendLine
              key={backend.id}
              backend={backend}
              busy={busy}
              onEdit={setEditing}
              onDelete={(target) => dropBackend.mutate(target.id)}
            />
          ))
        )}
        <Group>
          <Button
            size="xs"
            disabled={busy}
            onClick={() => setAddingBackend(true)}
            data-testid="zone-add-backend"
          >
            Add a provider
          </Button>
        </Group>
      </Stack>

      <Stack gap="xs">
        <Group justify="space-between" align="baseline">
          <span className="ddns-label">names in this zone</span>
          {canReadNames ? (
            <Anchor href={NAMES_PATH} size="sm" data-testid="zone-names-link">
              Manage names
            </Anchor>
          ) : null}
        </Group>
        {!canReadNames ? (
          <Text size="sm" data-testid="zone-names-refused">
            Reading the names in this zone needs the{' '}
            <code>{HOSTNAME_PERMISSION}</code> permission. This is a refusal,
            not an empty zone.
          </Text>
        ) : hostnames.isLoading ? (
          <Text size="sm" data-testid="zone-names-loading">
            Loading…
          </Text>
        ) : hostnames.error ? (
          <Alert color="gray" variant="light" data-testid="zone-names-error">
            <Text size="sm" ff="monospace">
              {(hostnames.error as Error).message}
            </Text>
          </Alert>
        ) : (
          <ZoneNames hostnames={zoneNames} />
        )}
      </Stack>

      <Modal
        opened={addingBackend}
        onClose={() => setAddingBackend(false)}
        title={`Add a provider to ${domain.name}`}
      >
        <BackendForm
          providers={providers.data ?? []}
          busy={busy}
          onCancel={() => setAddingBackend(false)}
          onSubmit={(value) =>
            addBackend.mutate({ domainId: domain.id, value })
          }
        />
      </Modal>

      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title="Edit provider binding"
      >
        {editing ? (
          <BackendForm
            providers={providers.data ?? []}
            existing={editing}
            busy={busy}
            onCancel={() => setEditing(null)}
            onSubmit={(value) => editBackend.mutate({ id: editing.id, value })}
          />
        ) : null}
      </Modal>

      <Modal
        opened={renaming}
        onClose={() => setRenaming(false)}
        title={`Rename ${domain.name}`}
      >
        <Stack gap="sm">
          <input
            className="ddns-data"
            aria-label="New zone name"
            value={newZoneName}
            onChange={(event) => setNewZoneName(event.currentTarget.value)}
            data-testid="rename-zone-name"
          />
          <Text size="xs" c="dimmed" data-testid="rename-zone-warning">
            {/* The rule, stated before the refusal rather than after it.
                `hostname_count` is the server's own count for this zone,
                so the sentence cannot claim a number the page does not
                show. */}
            {domain.hostname_count === 0
              ? 'This zone has no names in it, so any free zone name is accepted.'
              : `This zone has ${domain.hostname_count} name${
                  domain.hostname_count === 1 ? '' : 's'
                } under it. A rename that would leave any of them outside the zone is refused rather than rewriting them — a hostname is the DNS name a provider has already published a record under. Delete the ones you no longer want first.`}
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              variant="default"
              disabled={busy}
              onClick={() => setRenaming(false)}
              data-testid="rename-zone-cancel"
            >
              Cancel
            </Button>
            <Button
              size="xs"
              disabled={
                busy ||
                newZoneName.trim() === '' ||
                newZoneName.trim() === domain.name
              }
              onClick={() =>
                renameZone.mutate({ id: domain.id, name: newZoneName.trim() })
              }
              data-testid="rename-zone-submit"
            >
              Rename
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={`Delete ${domain.name}?`}
      >
        <Stack gap="sm">
          <Text size="sm" data-testid="delete-zone-warning">
            This destroys the zone, its {domain.backends.length} provider
            binding{domain.backends.length === 1 ? '' : 's'} — stored
            credentials included — and the {domain.hostname_count} name
            {domain.hostname_count === 1 ? '' : 's'} under it. It does{' '}
            <strong>not</strong> remove whatever your DNS provider has already
            published for those names.
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={busy}
              onClick={() => dropZone.mutate(domain.id)}
              data-testid="delete-zone-confirm"
            >
              Delete
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}

/** The modal entrance — §17's *"click a row → a modal opens"*.
 *
 * The `size` is `cards.ts`'s derived width and not a Mantine keyword.
 * `lg` is 620px, which is the drawer §12 rejected for being below the
 * one-strip minimum; reaching for it here would have reintroduced that
 * exact failure inside the shape the operator asked for.
 */
export function ZoneCardModal({
  zoneId,
  onClose,
}: {
  zoneId: number | null;
  onClose: () => void;
}) {
  return (
    <Modal
      opened={zoneId !== null}
      onClose={onClose}
      title="Zone"
      size={CARD_MODAL_WIDTH_PX}
      padding={CARD_MODAL_PADDING_PX}
      data-testid="zone-card-modal"
    >
      {/* The modal is portalled to `document.body`, outside the
          `data-ddns-root` div, so without this the whole card renders
          with none of `ddns.css` applied — see `DdnsPortalScope`. */}
      <DdnsPortalScope>
        {zoneId === null ? null : (
          <ZoneCard zoneId={zoneId} onDeleted={onClose} />
        )}
      </DdnsPortalScope>
    </Modal>
  );
}
