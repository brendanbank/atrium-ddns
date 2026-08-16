/** Zones, and the provider bindings under them.
 *
 * The zone is the row and the backends are nested under it, because a
 * backend has no meaning apart from the zone it writes to — a flat list
 * of bindings would make the reader carry the join.
 *
 * Every credential decision arrives computed. `credentials_set` is a
 * boolean read off `credentials_ct IS NOT NULL`; there is no masked
 * value to render, and there is deliberately nothing here that displays
 * a prefix or a length. `known_service` is the server's answer to
 * *"does this build have an adapter for it"*, and a row that says
 * `false` is rendered as what it is — a binding that can only answer
 * `911` — rather than as one that looks like it works.
 */
import { useState } from 'react';
import { Alert, Button, Group, Modal, Stack, Text } from '@mantine/core';
import { useMutation, useQueryClient } from '@tanstack/react-query';

import {
  DOMAINS_QUERY_KEY,
  createBackend,
  createDomain,
  deleteBackend,
  deleteDomain,
  renameDomain,
  updateBackend,
  type Domain,
  type DomainBackend,
  type Provider,
} from '../api/domains';
import { BackendForm, type BackendFormValue } from './BackendForm';

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
            backend nobody has finished configuring, and it is not the
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

export function DomainList({
  domains,
  providers,
}: {
  domains: Domain[];
  providers: Provider[];
}) {
  const client = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [zoneName, setZoneName] = useState('');
  const [backendFor, setBackendFor] = useState<Domain | null>(null);
  const [editing, setEditing] = useState<DomainBackend | null>(null);
  const [error, setError] = useState<string | null>(null);
  /* #75's rename. Two pieces of state rather than one, because the
     modal has to keep showing the zone it is renaming *and* what has
     been typed, and reusing `zoneName` would have the create form and
     the rename form share a text box. */
  const [renaming, setRenaming] = useState<Domain | null>(null);
  const [newZoneName, setNewZoneName] = useState('');

  const invalidate = () =>
    client.invalidateQueries({ queryKey: DOMAINS_QUERY_KEY });
  const fail = (err: Error) => setError(err.message);
  const done = () => {
    setError(null);
    setBackendFor(null);
    setEditing(null);
    setAdding(false);
    setZoneName('');
    setRenaming(null);
    setNewZoneName('');
    void invalidate();
  };

  const addZone = useMutation({
    mutationFn: createDomain,
    onSuccess: done,
    onError: fail,
  });
  const dropZone = useMutation({
    mutationFn: deleteDomain,
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

  const busy =
    addZone.isPending ||
    dropZone.isPending ||
    renameZone.isPending ||
    addBackend.isPending ||
    editBackend.isPending ||
    dropBackend.isPending;

  return (
    <Stack gap="lg">
      {error ? (
        <Alert
          color="gray"
          variant="light"
          title="That did not work"
          data-testid="domain-error"
        >
          <Text size="sm" ff="monospace">
            {error}
          </Text>
        </Alert>
      ) : null}

      {domains.length === 0 ? (
        <Text size="sm" data-testid="domains-empty">
          You have no zones yet. Add one, then bind a DNS provider to it.
        </Text>
      ) : (
        domains.map((domain) => (
          <Stack gap="xs" key={domain.id} data-testid={`domain-${domain.name}`}>
            <Group gap="sm">
              <span className="ddns-data">{domain.name}</span>
              <span className="ddns-label">
                {domain.hostname_count} hostname
                {domain.hostname_count === 1 ? '' : 's'}
              </span>
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => setBackendFor(domain)}
                data-testid={`add-backend-${domain.name}`}
              >
                Add a provider
              </Button>
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => {
                  setRenaming(domain);
                  setNewZoneName(domain.name);
                }}
                data-testid={`rename-domain-${domain.name}`}
              >
                Rename
              </Button>
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => dropZone.mutate(domain.id)}
                data-testid={`delete-domain-${domain.name}`}
              >
                Delete zone
              </Button>
            </Group>
            {domain.backends.length === 0 ? (
              <Text size="xs" c="dimmed">
                {/* A zone with no backend is a real configuration state
                    and the wire says so: every update against it
                    answers 911. Said here rather than left as an empty
                    space. */}
                No provider bound yet — updates for this zone answer 911.
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
          </Stack>
        ))
      )}

      <Group>
        <Button size="xs" onClick={() => setAdding(true)} data-testid="add-domain">
          Add a zone
        </Button>
      </Group>

      <Modal opened={adding} onClose={() => setAdding(false)} title="Add a zone">
        <Stack gap="sm">
          <input
            className="ddns-data"
            aria-label="Zone name"
            value={zoneName}
            onChange={(event) => setZoneName(event.currentTarget.value)}
            data-testid="zone-name"
          />
          <Text size="xs" c="dimmed">
            Stored lower-cased and without a trailing dot. Zone names are unique
            across the whole installation — DNS is global — so a name someone
            else already runs is refused.
          </Text>
          <Group justify="flex-end">
            <Button
              size="xs"
              disabled={zoneName.trim() === '' || busy}
              onClick={() => addZone.mutate(zoneName.trim())}
              data-testid="zone-submit"
            >
              Create
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={renaming !== null}
        onClose={() => setRenaming(null)}
        title={`Rename ${renaming?.name ?? ''}`}
      >
        {renaming ? (
          <Stack gap="sm">
            <input
              className="ddns-data"
              aria-label="New zone name"
              value={newZoneName}
              onChange={(event) => setNewZoneName(event.currentTarget.value)}
              data-testid="rename-zone-name"
            />
            <Text size="xs" c="dimmed" data-testid="rename-zone-warning">
              {/* The rule, stated before the refusal rather than after
                  it. `hostname_count` is the server's own count for this
                  zone, so the sentence cannot claim a number the list
                  does not show. */}
              {renaming.hostname_count === 0
                ? 'This zone has no names in it, so any free zone name is accepted.'
                : `This zone has ${renaming.hostname_count} name${
                    renaming.hostname_count === 1 ? '' : 's'
                  } under it. A rename that would leave any of them outside the zone is refused rather than rewriting them — a hostname is the DNS name a provider has already published a record under. Delete the ones you no longer want first.`}
            </Text>
            <Group justify="flex-end">
              <Button
                size="xs"
                variant="default"
                disabled={busy}
                onClick={() => setRenaming(null)}
                data-testid="rename-zone-cancel"
              >
                Cancel
              </Button>
              <Button
                size="xs"
                disabled={
                  busy ||
                  newZoneName.trim() === '' ||
                  newZoneName.trim() === renaming.name
                }
                onClick={() =>
                  renameZone.mutate({
                    id: renaming.id,
                    name: newZoneName.trim(),
                  })
                }
                data-testid="rename-zone-submit"
              >
                Rename
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>

      <Modal
        opened={backendFor !== null}
        onClose={() => setBackendFor(null)}
        title={`Add a provider to ${backendFor?.name ?? ''}`}
      >
        {backendFor ? (
          <BackendForm
            providers={providers}
            busy={busy}
            onCancel={() => setBackendFor(null)}
            onSubmit={(value) =>
              addBackend.mutate({ domainId: backendFor.id, value })
            }
          />
        ) : null}
      </Modal>

      <Modal
        opened={editing !== null}
        onClose={() => setEditing(null)}
        title="Edit provider binding"
      >
        {editing ? (
          <BackendForm
            providers={providers}
            existing={editing}
            busy={busy}
            onCancel={() => setEditing(null)}
            onSubmit={(value) => editBackend.mutate({ id: editing.id, value })}
          />
        ) : null}
      </Modal>
    </Stack>
  );
}
