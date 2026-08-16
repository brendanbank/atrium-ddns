/** One group of settings, rendered from the served schema.
 *
 * Nothing in this file knows the name of a setting. The inputs, their
 * bounds, their help text and their defaults all arrive from
 * `GET /atrium_ddns/config/schema`, which is derived from the same
 * Pydantic model atrium validates the PUT against — so the bounds the
 * form offers and the bounds the server enforces cannot drift.
 *
 * ## Why the bounds are on the input at all, given the server checks
 *
 * They are not the control; the server is. `min` on a `NumberInput`
 * stops a slip, and the sentence under the box says what the value
 * does. The reason it is worth wiring rather than leaving to the 400 is
 * the one AC 2 names: *a settings form that lets an operator write `0`
 * into `health_check_batch_size` has replaced a bad UX with an outage*
 * — and while the model does refuse it (`ge=1`, swept in
 * `tests/test_settings_schema.py`), a form that offers it teaches the
 * operator that the number is a choice.
 *
 * ## A type this file cannot render is a refusal, not a skipped row
 *
 * `unknownField` renders a named refusal. Dropping the row would leave
 * a setting that exists, validates, enforces and is uneditable — the
 * exact state #73 was opened about, reintroduced one layer down.
 */
import { NumberInput, Switch, Text, TextInput } from '@mantine/core';

import type { SettingField } from '../api/config';

/** What a field's value can be over the wire. */
export type SettingValue = string | number | boolean | null;

export function isNumeric(field: SettingField): boolean {
  return field.type === 'integer' || field.type === 'number';
}

/** The help line under an input: the model's sentence, then the range
 *  and the default. The range is printed because "1 to 10 000" is the
 *  answer to the question the box provokes, and the default because
 *  every per-device limit in this product can *inherit* it and there is
 *  otherwise nowhere to read the inherited number. */
export function fieldHelp(field: SettingField): string {
  const parts = [field.help];
  if (field.minimum !== null && field.maximum !== null) {
    parts.push(`${field.minimum} to ${field.maximum}.`);
  } else if (field.minimum !== null) {
    parts.push(`At least ${field.minimum}.`);
  } else if (field.maximum !== null) {
    parts.push(`At most ${field.maximum}.`);
  }
  if (field.type !== 'boolean') {
    parts.push(`Default ${String(field.default)}.`);
  }
  return parts.filter((part) => part.length > 0).join(' ');
}

export function SettingInput({
  field,
  value,
  disabled,
  onChange,
}: {
  field: SettingField;
  value: SettingValue;
  disabled: boolean;
  onChange: (next: SettingValue) => void;
}) {
  const testid = `setting-${field.name}`;
  if (field.type === 'boolean') {
    return (
      <Switch
        label={field.label}
        description={fieldHelp(field)}
        checked={value === true}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        data-testid={testid}
      />
    );
  }
  if (isNumeric(field)) {
    return (
      <NumberInput
        label={field.label}
        description={fieldHelp(field)}
        value={typeof value === 'number' ? value : ''}
        disabled={disabled}
        // `?? undefined` and not `?? 0`: an absent bound is *unbounded*,
        // and a 0 floor on a field with no floor is a bound this form
        // invented.
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        // The float field is the reason `integer` and `number` are
        // carried separately all the way from the model. An input that
        // rounded `health_check_timeout_seconds` to 5 would silently
        // change the operator's setting.
        allowDecimal={field.type === 'number'}
        step={field.type === 'number' ? 0.1 : 1}
        onChange={(next) =>
          onChange(typeof next === 'number' ? next : Number(next))
        }
        data-testid={testid}
      />
    );
  }
  if (field.type === 'string') {
    return (
      <TextInput
        label={field.label}
        description={fieldHelp(field)}
        value={typeof value === 'string' ? value : ''}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value)}
        data-testid={testid}
      />
    );
  }
  return (
    <Text size="sm" data-testid={`setting-unrenderable-${field.name}`}>
      <code>{field.name}</code> is a <code>{field.type}</code>, which this
      build has no input for. It is listed rather than hidden: a setting
      nobody can see is the thing this page exists to stop. Change it with{' '}
      <code>PUT /api/admin/app-config/atrium_ddns</code> until the form
      grows a renderer.
    </Text>
  );
}
