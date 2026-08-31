/** No React synthetic event may be read from inside a functional `setState`.
 *
 * ## The defect this guards, stated once
 *
 * ```tsx
 * onChange={(event) =>
 *   setSecrets((current) => ({ ...current, [key]: event.currentTarget.value }))
 * }
 * ```
 *
 * React runs a functional updater **eagerly** only when the fiber has no
 * update already pending. Otherwise it defers it to the render phase —
 * and by then `executeDispatch`'s `finally` has set
 * `event.currentTarget` back to `null`. The lazy spelling therefore
 * throws `Cannot read properties of null (reading 'value')` *during
 * render*, and an error thrown during render with no error boundary over
 * the host root makes React unmount **the entire root**. The dialog and
 * the page behind it disappear together, `main` is left empty, and the
 * next thing to look at the page reports a missing element.
 *
 * ## Why a source guard and not a test that runs it
 *
 * Whether an update is already pending depends on whether a react-query
 * refetch happened to land in the same tick, so the failure is
 * intermittent by construction and gets *more* likely the busier the
 * machine is. `resolution-strip.spec.ts` caught it at roughly two runs in
 * three; three separate readers of that report filed it as a one-off.
 * There is no assertion that makes the runtime failure deterministic, so
 * the invariant is checked where it *is* deterministic — in the text.
 *
 * ## Why it is a guard rather than a note
 *
 * It has already recurred. `BackendForm.tsx` carries the diagnosis and
 * the fix in a comment from the first time; `ZoneModal.tsx` was written
 * from that form and kept the defect in two places (#122). A comment in
 * one file does not travel to its copy.
 *
 * Same rule as `design.test.ts`: read the shipped files, derive rather
 * than restate, and assert the scan found *something* so a broken
 * matcher cannot pass by matching nothing.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

const SRC = resolve(process.cwd(), 'src');

/** Every `.ts`/`.tsx` this bundle ships, tests excluded — a test file is
 *  allowed to contain the bad pattern as a fixture, and this one does. */
function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === 'test') continue;
      out.push(...sourceFiles(full));
      continue;
    }
    if (!/\.tsx?$/.test(entry)) continue;
    if (/\.test\.tsx?$/.test(entry)) continue;
    out.push(full);
  }
  return out;
}

/** The names a React synthetic event is reached through, and which are
 *  detached the instant the handler returns. `target` is deliberately
 *  absent: it is a plain DOM node reference and stays valid. */
const DETACHED_MEMBERS = ['currentTarget', 'nativeEvent'];

interface Offence {
  file: string;
  setter: string;
  snippet: string;
}

/** Find `setSomething(<arrow function containing a detached member>)`.
 *
 * Balanced-paren scan rather than a regex over the whole call: the
 * argument is an arrow returning an object literal, so it carries its
 * own brackets and a lazy `.*?` would stop at the first `)` inside it.
 */
export function findEventReadsInUpdaters(source: string, file = ''): Offence[] {
  const offences: Offence[] = [];
  const opener = /\bset[A-Z][A-Za-z0-9_$]*\(/g;
  let match: RegExpExecArray | null;
  while ((match = opener.exec(source)) !== null) {
    let depth = 1;
    let i = match.index + match[0].length;
    while (i < source.length && depth > 0) {
      if (source[i] === '(') depth += 1;
      else if (source[i] === ')') depth -= 1;
      i += 1;
    }
    const arg = source.slice(match.index + match[0].length, i - 1);
    // Only *functional* updaters are at risk. `setName(e.currentTarget.value)`
    // reads the event while the handler is still on the stack, which is
    // the correct spelling and must not be flagged.
    if (!arg.includes('=>')) continue;
    const member = DETACHED_MEMBERS.find((m) => arg.includes(m));
    if (!member) continue;
    offences.push({
      file,
      setter: match[0].slice(0, -1),
      snippet: arg.replace(/\s+/g, ' ').slice(0, 120),
    });
  }
  return offences;
}

describe('a synthetic event is never read from inside a functional setState', () => {
  const files = sourceFiles(SRC);

  test('the scan reaches the files it claims to', () => {
    // Vacuity check, and not a formality: `design.test.ts` records a
    // sibling guard that passed for a while against an empty string.
    expect(files.length).toBeGreaterThan(20);
    expect(files.map((f) => relative(SRC, f))).toContain(
      join('tenant', 'ZoneModal.tsx'),
    );
  });

  test('the detector fires on the shape it is written for', () => {
    // The guard shown failing, in the file that owns it. This is the
    // exact text `ZoneModal.tsx` shipped before #122.
    const bad =
      'onChange={(event) =>\n' +
      '  setSecrets((current) => ({ ...current, [key]: event.currentTarget.value }))\n' +
      '}';
    expect(findEventReadsInUpdaters(bad).map((o) => o.setter)).toEqual([
      'setSecrets',
    ]);
    // And does not fire on the correct spelling, which reads the event
    // while the handler still owns it.
    const good =
      'onChange={(event) => {\n' +
      '  const value = event.currentTarget.value;\n' +
      '  setSecrets((current) => ({ ...current, [key]: value }));\n' +
      '}}';
    expect(findEventReadsInUpdaters(good)).toEqual([]);
    // Nor on the direct form, which is not an updater at all.
    expect(
      findEventReadsInUpdaters('setName(event.currentTarget.value)'),
    ).toEqual([]);
  });

  test('functional updaters exist in this tree at all', () => {
    // Second vacuity check. If the balanced-paren walk broke, the
    // assertion below would pass by finding nothing anywhere.
    const anyUpdater = files.some((f) =>
      /\bset[A-Z][A-Za-z0-9_$]*\(\s*\(?[A-Za-z_$]*\)?\s*=>/.test(
        readFileSync(f, 'utf8'),
      ),
    );
    expect(anyUpdater).toBe(true);
  });

  test('no shipped file reads one', () => {
    const offences = files.flatMap((f) =>
      findEventReadsInUpdaters(readFileSync(f, 'utf8'), relative(SRC, f)),
    );
    expect(
      offences.map((o) => `${o.file}: ${o.setter}(${o.snippet})`),
    ).toEqual([]);
  });
});
