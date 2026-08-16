/** The operator-configuration pages — `registerSettingsGroup`'s children.
 *
 * #73, and plan §4's one unbuilt clause: *"Rate limits, health-check
 * config, retention become one nested group via `registerSettingsGroup`
 * rather than sibling pages."* Three pages, one group, and every setting
 * that were reachable only by `curl` until now — including
 * `rate_limit_per_minute`, which is the abuse control on a public
 * endpoint authenticated by HTTP Basic and nothing else.
 *
 * ## Three things this page refuses to do quietly
 *
 * **1. It does not lose a setting.** The field list is the served
 * schema's, and the schema's population is `DdnsConfig.model_fields`.
 * A group the server sends that this build has no page for is named on
 * screen (`settings-unrouted`) rather than dropped — see
 * `settings/settingsRoutes.ts` for why that seam exists at all.
 *
 * **2. It does not send a partial namespace.** Atrium's
 * `put_namespace` validates the whole model, so a body that omits a
 * field resets it to the model default. Saving one page therefore
 * writes *every* value the read returned, edited ones replaced — which
 * is why the values query is the whole namespace and not this group's
 * slice.
 *
 * **3. It tells "no namespace" apart from "all defaults".** If
 * `GET /admin/app-config` carries no `atrium_ddns` key, the running
 * atrium never imported the host module that registers it. Rendering
 * the model defaults there would be a form whose save button 404s, so the
 * page says what happened instead.
 */
import { useMemo, useState } from 'react';
import { Alert, Button, Group, Stack, Text, Title } from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePerm } from '@brendanbank/atrium-host-bundle-utils/react';

import {
  CONFIG_PERMISSION,
  NAMESPACE,
  VALUES_QUERY_KEY,
  namespaceValuesQuery,
  putNamespaceValues,
  settingsSchemaQuery,
  type NamespaceValues,
  type SettingGroup,
} from './api/config';
import { DdnsRoot } from './host/DdnsRoot';
import {
  SettingInput,
  type SettingValue,
} from './settings/SettingsFields';
import { SETTINGS_ROUTES } from './settings/settingsRoutes';

export function SettingsInner({ groupKey }: { groupKey: string }) {
  const hasPerm = usePerm();
  const canManage = hasPerm(CONFIG_PERMISSION);
  const client = useQueryClient();

  const schema = useQuery(settingsSchemaQuery({ enabled: canManage }));
  const values = useQuery(namespaceValuesQuery({ enabled: canManage }));

  // Edits live here and nowhere else. Keyed by field name across the
  // whole namespace, not by this group — the save writes all of it.
  const [edits, setEdits] = useState<Record<string, SettingValue>>({});
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const group: SettingGroup | undefined = useMemo(
    () => schema.data?.groups.find((entry) => entry.key === groupKey),
    [schema.data, groupKey],
  );

  /** Groups the server sent that this build cannot route to. Computed
   *  from the served schema against the route map, so it is a
   *  measurement rather than a list somebody remembered to update. */
  const unrouted = useMemo(
    () =>
      (schema.data?.groups ?? []).filter(
        (entry) => SETTINGS_ROUTES[entry.key] === undefined,
      ),
    [schema.data],
  );

  const stored: NamespaceValues | null | undefined = values.data;

  const save = useMutation({
    mutationFn: async () => {
      if (!schema.data || !stored) {
        throw new Error('nothing loaded to save');
      }
      // Every field the schema knows, taking the edit if there is one,
      // then the stored value, then the model default. The last fallback
      // matters for a field added to the model since the row was
      // written: `get_namespace` re-applies defaults on read, so it
      // should never fire — and if it does, sending the default is the
      // same thing the server would have stored anyway.
      const body: NamespaceValues = {};
      for (const entry of schema.data.groups) {
        for (const field of entry.fields) {
          body[field.name] =
            field.name in edits
              ? edits[field.name]
              : field.name in stored
                ? stored[field.name]
                : field.default;
        }
      }
      return putNamespaceValues(schema.data.write_path, body);
    },
    onSuccess: () => {
      setEdits({});
      setError(null);
      setSaved(true);
      void client.invalidateQueries({ queryKey: VALUES_QUERY_KEY });
    },
    onError: (err: Error) => {
      setSaved(false);
      setError(err.message);
    },
  });

  const dirty = Object.keys(edits).length > 0;

  if (!canManage) {
    return (
      <Stack gap="md">
        <Title order={3}>Configuration</Title>
        <Alert
          color="gray"
          variant="light"
          title="Not available to this account"
          data-testid="settings-refused"
        >
          <Text size="sm">
            Changing installation settings needs the{' '}
            <code>{CONFIG_PERMISSION}</code> permission — atrium&rsquo;s own,
            the same one its System and Auth sections use. This is a refusal,
            not an empty page.
          </Text>
        </Alert>
      </Stack>
    );
  }

  if (schema.isLoading || values.isLoading) {
    return (
      <Stack gap="md">
        <Title order={3}>Configuration</Title>
        <Text size="sm" data-testid="settings-loading">
          Loading…
        </Text>
      </Stack>
    );
  }

  if (schema.error || values.error) {
    return (
      <Stack gap="md">
        <Title order={3}>Configuration</Title>
        <Alert
          color="gray"
          variant="light"
          title="Could not load the settings"
          data-testid="settings-error"
        >
          {/* Diagnostics in full — the status and the server's own
              words. Redact secrets, never diagnostics. */}
          <Text size="sm" ff="monospace">
            {((schema.error ?? values.error) as Error).message}
          </Text>
        </Alert>
      </Stack>
    );
  }

  if (stored === undefined || schema.data === undefined) {
    // Not loading, no error, and nothing arrived. There is no known way
    // to reach this — it is here because the alternative is rendering a
    // form over `undefined`, and "the page was blank" is the least
    // diagnosable bug report there is.
    return (
      <Stack gap="md">
        <Title order={3}>Configuration</Title>
        <Alert
          color="gray"
          variant="light"
          title="Nothing loaded, and nothing failed"
          data-testid="settings-unavailable"
        >
          <Text size="sm">
            The settings queries resolved with neither data nor an error.
            Reload the page; if it persists, this is a bug in the host bundle
            rather than a refusal or an outage.
          </Text>
        </Alert>
      </Stack>
    );
  }

  if (stored === null) {
    return (
      <Stack gap="md">
        <Title order={3}>Configuration</Title>
        <Alert
          color="gray"
          variant="light"
          title="This installation serves no atrium_ddns settings"
          data-testid="settings-absent"
        >
          <Text size="sm">
            <code>GET /api/admin/app-config</code> answered without an{' '}
            <code>{NAMESPACE}</code> namespace, which means the running atrium
            never imported the host module that registers it. Nothing here can
            be saved until it does; the values the service is using are the
            model defaults.
          </Text>
        </Alert>
      </Stack>
    );
  }

  return (
    <Stack gap="md">
      <Title order={3}>{group?.label ?? 'Configuration'}</Title>

      {unrouted.length > 0 ? (
        <Alert
          color="gray"
          variant="light"
          title="Settings with no page in this build"
          data-testid="settings-unrouted"
        >
          <Text size="sm">
            The API groups these settings under{' '}
            {unrouted.map((entry) => entry.key).join(', ')}, and this build
            registers no page for them:{' '}
            {unrouted
              .flatMap((entry) => entry.fields.map((field) => field.name))
              .join(', ')}
            . They are named here rather than hidden — the frontend and the
            backend disagree about the grouping, and until they are brought
            back together those settings are reachable only with{' '}
            <code>PUT /api/admin/app-config/{NAMESPACE}</code>.
          </Text>
        </Alert>
      ) : null}

      {group === undefined ? (
        <Alert
          color="gray"
          variant="light"
          title="No such settings group"
          data-testid="settings-missing-group"
        >
          <Text size="sm">
            This page is registered for the group <code>{groupKey}</code> and
            the API serves no group by that name. It serves:{' '}
            {(schema.data?.groups ?? []).map((entry) => entry.key).join(', ')}.
          </Text>
        </Alert>
      ) : (
        <>
          <Text size="sm" c="dimmed" data-testid="settings-blurb">
            {group.blurb}
          </Text>

          {error ? (
            <Alert
              color="gray"
              variant="light"
              title="That did not save"
              data-testid="settings-save-error"
            >
              <Text size="sm" ff="monospace">
                {error}
              </Text>
            </Alert>
          ) : null}

          {saved && !dirty ? (
            <Text size="sm" data-testid="settings-saved">
              Saved. The service reads these on its next tick — nothing needs
              restarting.
            </Text>
          ) : null}

          <Stack gap="sm">
            {group.fields.map((field) => (
              <SettingInput
                key={field.name}
                field={field}
                value={
                  (field.name in edits
                    ? edits[field.name]
                    : (stored[field.name] as SettingValue)) ?? null
                }
                disabled={save.isPending}
                onChange={(next) => {
                  setSaved(false);
                  setEdits((current) => ({ ...current, [field.name]: next }));
                }}
              />
            ))}
          </Stack>

          <Group>
            <Button
              size="xs"
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate()}
              data-testid="settings-save"
            >
              Save
            </Button>
            <Button
              size="xs"
              variant="default"
              disabled={!dirty || save.isPending}
              onClick={() => {
                setEdits({});
                setError(null);
              }}
              data-testid="settings-discard"
            >
              Discard changes
            </Button>
          </Group>
        </>
      )}
    </Stack>
  );
}

export function SettingsPage({ groupKey }: { groupKey: string }) {
  return (
    <DdnsRoot>
      <SettingsInner groupKey={groupKey} />
    </DdnsRoot>
  );
}
