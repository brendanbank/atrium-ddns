/** The client half of the blank-preserves rule.
 *
 * `apply_secret_update` on the server reads three values and three only:
 *
 * | wire value | meaning |
 * |---|---|
 * | `null` | **clear** the stored credential |
 * | `""` (and an absent field) | **preserve** it |
 * | an object | **replace** it |
 *
 * The failure this file exists to prevent is not the server getting that
 * wrong — the server uses atrium's own helper and
 * `backend/tests/test_router_tenant.py` asserts all three directions.
 * It is **the form sending the wrong one of the three**, which is a
 * different bug with the same consequence: a user who opens a credential
 * form to change a TTL, leaves the secret boxes alone, saves, and
 * silently blanks a working provider credential. It surfaces days later
 * as a DNS update failing with nothing pointing at the cause.
 *
 * ## The rule this module enforces
 *
 * > **A text box the user did not type in is not a value. It is an
 * > absence, and the form must say which kind of absence it is.**
 *
 * An empty `<input>` is ambiguous — *I did not touch this* and *I want
 * this gone* look identical — so the form never derives the payload from
 * emptiness. It derives it from an explicit `CredentialMode`, and the
 * three modes map onto the three wire values one for one. `buildPayload`
 * is total over the mode, so a fourth mode added later fails `tsc`
 * rather than falling through to a default.
 *
 * `""` is therefore *never* produced from a field's contents. It is
 * produced by the `keep` mode and by nothing else, which is exactly the
 * distinction the issue asks for: **the form must not send `""` meaning
 * "empty"**.
 */

/** What the user has said about the stored credential.
 *
 * Named states rather than a boolean pair, because "is it being
 * replaced" and "is it being cleared" are not independent and a pair
 * admits a fourth combination that means nothing.
 */
export type CredentialMode = 'keep' | 'replace' | 'clear';

/** The wire shape. `""` and `null` are values, not sentinels for
 *  "missing" — that is the whole point of the rule. */
export type CredentialsPayload = Record<string, string> | '' | null;

/** Raised for a `replace` the form should not have submitted.
 *
 * A refusal rather than a coerced payload. Sending a partial set would
 * store a backend that answers `dnserr` on every update, and sending a
 * blank value would replace a working credential with nothing — the two
 * failures this module exists to prevent, arriving through the object
 * rather than through the field.
 */
export class CredentialFormError extends Error {
  /** The offending field names. Never their values: an error message
   *  reaches the DOM, and a credential in the DOM is a credential in a
   *  screenshot. */
  readonly fields: string[];

  constructor(message: string, fields: string[]) {
    super(message);
    this.name = 'CredentialFormError';
    this.fields = fields;
  }
}

/** The value that means *preserve*. Exported so a test can assert
 *  against the same string the payload carries rather than a literal
 *  typed twice. */
export const PRESERVE = '' as const;

/** Turn the form's state into the wire value.
 *
 * `keys` is the provider's own `credential_keys`, which arrives from
 * `GET /providers` and is derived server-side from the provider class.
 * Passing it in (rather than reading `Object.keys(values)`) is what
 * makes a *missing* field detectable: a form that never rendered a box
 * for `aws_secret_access_key` would otherwise submit a complete-looking
 * object with one key in it.
 */
export function buildCredentialsPayload(
  mode: CredentialMode,
  keys: readonly string[],
  values: Readonly<Record<string, string>>,
): CredentialsPayload {
  switch (mode) {
    case 'keep':
      // The one place `""` is produced, and it is produced from the
      // *mode*, never from a field being empty.
      return PRESERVE;
    case 'clear':
      return null;
    case 'replace': {
      if (keys.length === 0) {
        throw new CredentialFormError(
          'this provider declares no credential fields, so there is nothing to replace',
          [],
        );
      }
      const missing = keys.filter((key) => (values[key] ?? '').trim() === '');
      if (missing.length > 0) {
        throw new CredentialFormError(
          `fill in every field, or choose “keep the stored credential”: ${missing.join(', ')}`,
          missing,
        );
      }
      // Trimmed, and only here. A leading space pasted from a terminal
      // is not part of an API key, and the server's own guard treats a
      // whitespace-only value as blank — so trimming client-side keeps
      // the two readings of "blank" identical.
      return Object.fromEntries(keys.map((key) => [key, values[key].trim()]));
    }
  }
}

/** The default mode for a form opened against an existing row.
 *
 * `keep` when something is stored, `replace` when nothing is — so the
 * *safe* option is the default in the case where getting it wrong
 * destroys something, and the *useful* option is the default in the case
 * where there is nothing to destroy. A form that defaulted to `replace`
 * throughout would put the destructive path one accidental save away.
 */
export function defaultCredentialMode(credentialsSet: boolean): CredentialMode {
  return credentialsSet ? 'keep' : 'replace';
}
