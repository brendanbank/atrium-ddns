/** The `atrium_ddns` config namespace — read, describe, write.
 *
 * #73. The namespace has been served by atrium's admin API since #17
 * and no screen could reach it. The measurement, taken from the shell
 * bundle the stack serves rather than from atrium's source:
 *
 * ```
 * 'atrium_ddns' anywhere in the SHELL bundle : 0 occurrences
 * the namespace-parameterised mutation hook  : PUT /admin/app-config/${e}
 * every call site of it, exhaustively        : ['e', `auth`, `brand`, `system`, `i18n`]
 * ```
 *
 * The hook is generic and **every caller passes a literal**. Nothing in
 * atrium derives a namespace from what `GET /admin/app-config` returns,
 * so a namespace no screen names is unreachable however completely the
 * API serves it. This module is the host naming it.
 *
 * ## Three endpoints, two of them atrium's
 *
 * - `GET /admin/app-config` — atrium's. Every namespace, values only.
 * - `PUT /admin/app-config/atrium_ddns` — atrium's. Validated against
 *   the host's own Pydantic model.
 * - `GET /atrium_ddns/config/schema` — the host's. The **shape**:
 *   types, bounds, defaults and help text, derived from that same
 *   model. Atrium's PUT takes a bare `dict`, so the namespace's bounds
 *   appear nowhere in the OpenAPI document and a form that hardcoded
 *   them would be a second copy of the model with nothing able to see
 *   it drift.
 *
 * ## The PUT is whole-namespace, and that is not an optimisation
 *
 * `put_namespace` runs `model_validate(payload)`, so a body that omits
 * a field does not leave it alone — it **resets it to the model
 * default**. A form that PATCHed one field would silently revert every
 * other setting on the page. So the write always sends every value the
 * read returned, with the edited ones replaced.
 */
import { queryOptions } from '@tanstack/react-query';

import { apiGet, apiSend } from './http';

/** The namespace key, and the only place this bundle spells it. */
export const NAMESPACE = 'atrium_ddns';

/** Atrium's permission, not one of ours. The values live in atrium's
 *  `app_settings` table and are written through atrium's endpoint,
 *  which gates on exactly this — a host-specific permission would open
 *  a form whose save button answers 403. The backend's
 *  `settings_schema.APP_CONFIG_MANAGE_PERMISSION` is the same string;
 *  the served document carries it (`permission`) so a mismatch is
 *  visible rather than inferred. */
export const CONFIG_PERMISSION = 'app_setting.manage';

/** JSON-schema types the form knows how to render. `integer` and
 *  `number` are separate on purpose: `health_check_timeout_seconds` is
 *  a float and an input that rounds it to 5 has silently changed the
 *  operator's setting. */
export type SettingType = 'integer' | 'number' | 'boolean' | 'string';

export interface SettingField {
  name: string;
  type: SettingType;
  label: string;
  /** The model's `description`. The backend refuses an empty one. */
  help: string;
  default: unknown;
  /** `null` is *unbounded*, never `0`. */
  minimum: number | null;
  maximum: number | null;
}

export interface SettingGroup {
  key: string;
  label: string;
  blurb: string;
  fields: SettingField[];
}

export interface SettingsSchema {
  namespace: string;
  /** The atrium path the write goes to, carried rather than built here
   *  — the defect #73 measured in atrium's own shell is four call sites
   *  each spelling their namespace by hand. */
  write_path: string;
  permission: string;
  groups: SettingGroup[];
}

/** `GET /admin/app-config` returns every namespace; ours is one key. */
export type NamespaceValues = Record<string, unknown>;
export type AdminAppConfig = Record<string, NamespaceValues>;

export const SCHEMA_QUERY_KEY = ['atrium_ddns', 'config', 'schema'] as const;
export const VALUES_QUERY_KEY = ['atrium_ddns', 'config', 'values'] as const;

export async function getSettingsSchema(): Promise<SettingsSchema> {
  return apiGet<SettingsSchema>('/atrium_ddns/config/schema');
}

export function settingsSchemaQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: SCHEMA_QUERY_KEY,
    queryFn: getSettingsSchema,
    enabled: options.enabled,
    // The shape is a property of the build, not of the data. It cannot
    // change without a deploy, and a deploy reloads the bundle.
    staleTime: Infinity,
  });
}

/** Our namespace's stored values, out of atrium's admin bundle.
 *
 * Returns `null` — not `{}` — when the response has no `atrium_ddns`
 * key at all. That is a real state and a different one from "every
 * value is the default": it means the running atrium never imported the
 * host module that registers the namespace, and the form says so
 * instead of rendering a form of defaults nobody can save. */
export async function getNamespaceValues(): Promise<NamespaceValues | null> {
  const all = await apiGet<AdminAppConfig>('/admin/app-config');
  const mine = all[NAMESPACE];
  return mine === undefined ? null : mine;
}

export function namespaceValuesQuery(options: { enabled: boolean }) {
  return queryOptions({
    queryKey: VALUES_QUERY_KEY,
    queryFn: getNamespaceValues,
    enabled: options.enabled,
  });
}

/** Write the **whole** namespace. See the module docstring: a partial
 *  body resets what it omits. */
export async function putNamespaceValues(
  writePath: string,
  values: NamespaceValues,
): Promise<NamespaceValues> {
  return apiSend<NamespaceValues>(writePath, 'PUT', values);
}
