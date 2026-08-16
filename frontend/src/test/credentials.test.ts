/** The client half of the blank-preserves rule, all three directions.
 *
 * `apply_secret_update` reads three values — `null` clears, `""`
 * preserves, an object replaces — and the server side of that is
 * asserted in `backend/tests/test_router_tenant.py`. This file asserts
 * the half the server cannot see: **that the form sends the right one of
 * the three.**
 *
 * The specific defect the issue names is *"the form must not send `""`
 * meaning empty"*, and the test for it is not "does `keep` produce
 * `''`" — that passes on an implementation that also produces `''` from
 * an empty text box, which is the bug. It is
 * `test_only_keep_ever_produces_the_preserve_sentinel`: drive every
 * mode against every shape of field state, and assert that `''` comes
 * out of `keep` and out of nothing else.
 */
import { describe, expect, test } from 'vitest';

import {
  CredentialFormError,
  PRESERVE,
  buildCredentialsPayload,
  defaultCredentialMode,
  type CredentialMode,
} from '../api/credentials';

const KEYS = ['aws_access_key_id', 'aws_secret_access_key'] as const;

const FILLED = {
  aws_access_key_id: 'AKIAEXAMPLE',
  aws_secret_access_key: 'sekrit',
};

describe('the three directions', () => {
  test('keep preserves — and preserve is a value, not an omission', () => {
    expect(buildCredentialsPayload('keep', KEYS, FILLED)).toBe(PRESERVE);
    expect(PRESERVE).toBe('');
    // Not `undefined`: an omitted key and `""` happen to mean the same
    // thing to the server, but only because the schema defaults to
    // `""`. Sending the value makes the intent explicit on the wire and
    // survives a schema whose default changes.
    expect(buildCredentialsPayload('keep', KEYS, FILLED)).not.toBeUndefined();
  });

  test('clear clears, and clearing is spelled null', () => {
    expect(buildCredentialsPayload('clear', KEYS, FILLED)).toBeNull();
    // Even with fields filled in. The mode decides, not the fields —
    // which is the whole reason the mode exists.
    expect(buildCredentialsPayload('clear', KEYS, FILLED)).toBeNull();
  });

  test('replace replaces, with every declared key present', () => {
    expect(buildCredentialsPayload('replace', KEYS, FILLED)).toEqual(FILLED);
  });
});

describe('the empty text box', () => {
  test('only keep ever produces the preserve sentinel', () => {
    // The defect this file exists for, driven as a matrix rather than
    // as one assertion: every mode against every field state, and `''`
    // must come out of exactly one cell of it.
    const modes: CredentialMode[] = ['keep', 'replace', 'clear'];
    const states: Record<string, Record<string, string>> = {
      filled: FILLED,
      empty: { aws_access_key_id: '', aws_secret_access_key: '' },
      partial: { aws_access_key_id: 'AKIAEXAMPLE', aws_secret_access_key: '' },
      whitespace: { aws_access_key_id: '   ', aws_secret_access_key: '\t' },
      absent: {},
    };

    const producedPreserve: string[] = [];
    for (const mode of modes) {
      for (const [label, values] of Object.entries(states)) {
        let result: unknown;
        try {
          result = buildCredentialsPayload(mode, KEYS, values);
        } catch (error) {
          expect(error).toBeInstanceOf(CredentialFormError);
          continue;
        }
        if (result === PRESERVE) producedPreserve.push(`${mode}/${label}`);
      }
    }
    expect(producedPreserve).toEqual([
      'keep/filled',
      'keep/empty',
      'keep/partial',
      'keep/whitespace',
      'keep/absent',
    ]);
  });

  test('replace refuses a blank field rather than sending it', () => {
    // Sending it would replace a working credential with a blank one —
    // the exact failure blank-preserves exists to prevent, arriving one
    // level below the field that guards it.
    expect(() =>
      buildCredentialsPayload('replace', KEYS, {
        aws_access_key_id: 'AKIAEXAMPLE',
        aws_secret_access_key: '',
      }),
    ).toThrow(CredentialFormError);
  });

  test('replace refuses a whitespace-only field too', () => {
    // The server treats a whitespace-only value as blank, so the client
    // must as well — two readings of "blank" that disagree is how a
    // value gets past one guard and stopped by the other.
    expect(() =>
      buildCredentialsPayload('replace', KEYS, {
        aws_access_key_id: '   ',
        aws_secret_access_key: 'sekrit',
      }),
    ).toThrow(CredentialFormError);
  });

  test('replace refuses a key the form never rendered', () => {
    // `keys` comes from the provider, not from `Object.keys(values)`.
    // Derived that way, a form that failed to render a box for the
    // second key is a *missing field*; derived from the values, it
    // would be a complete-looking object with one key in it.
    expect(() =>
      buildCredentialsPayload('replace', KEYS, {
        aws_access_key_id: 'AKIAEXAMPLE',
      }),
    ).toThrow(CredentialFormError);
  });

  test('the refusal names the fields and never their values', () => {
    // An error message reaches the DOM, and a credential in the DOM is
    // a credential in a screenshot.
    try {
      buildCredentialsPayload('replace', KEYS, {
        aws_access_key_id: 'AKIAEXAMPLE',
        aws_secret_access_key: '',
      });
      throw new Error('expected a refusal');
    } catch (error) {
      expect(error).toBeInstanceOf(CredentialFormError);
      const failure = error as CredentialFormError;
      expect(failure.fields).toEqual(['aws_secret_access_key']);
      expect(failure.message).not.toContain('AKIAEXAMPLE');
    }
  });

  test('values are trimmed, and only on the replace path', () => {
    expect(
      buildCredentialsPayload('replace', KEYS, {
        aws_access_key_id: '  AKIAEXAMPLE ',
        aws_secret_access_key: 'sekrit\n',
      }),
    ).toEqual(FILLED);
  });
});

describe('the default mode', () => {
  test('is the safe one where getting it wrong destroys something', () => {
    // `keep` when a credential is stored, so an accidental save cannot
    // blank it; `replace` when nothing is stored, because there is
    // nothing to destroy and the useful default costs nothing.
    expect(defaultCredentialMode(true)).toBe('keep');
    expect(defaultCredentialMode(false)).toBe('replace');
  });
});

describe('a provider with no declared fields', () => {
  test('cannot be replaced, and says so rather than sending {}', () => {
    // `{}` is refused server-side as ambiguous between clear and
    // preserve. Producing it here would turn a form mistake into a 422
    // the user cannot act on.
    expect(() => buildCredentialsPayload('replace', [], {})).toThrow(
      CredentialFormError,
    );
    // …but keep and clear still mean what they mean.
    expect(buildCredentialsPayload('keep', [], {})).toBe(PRESERVE);
    expect(buildCredentialsPayload('clear', [], {})).toBeNull();
  });
});
