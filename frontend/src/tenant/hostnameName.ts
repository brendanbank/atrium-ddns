/** Turning what you typed into the name that is sent, and back.
 *
 * Moved out of `HostnameList` when `/atrium-ddns/names` was removed and
 * the board became the only tenant surface. The list went with the page;
 * these did not, because `NameModal` composes on the way in and
 * decomposes on the way out, and that pair is the thing the suite sweeps
 * for: **one home for the question "does this name already end with its
 * zone?"**. Two spellings of it disagree at the zone apex, which is how
 * the sweep found the second copy in the first place.
 */
/** The sentinel the create form is reached by. `NameModal` reads a
 *  `null` id as create; this list only knows "open something", so it
 *  hands the page a value the page maps. */
export const NEW_NAME = -1;

export function composeHostname(typed: string, zone: string | null): string {
  const entered = typed.trim();
  // Nothing typed is nothing to send — not the zone apex. An empty
  // field is an absence of input, and inventing `example.invalid` from
  // it would submit a name the operator never wrote.
  if (entered === '') return '';
  if (zone === null || zone === '') return entered;
  if (entered.toLowerCase().endsWith(zone.toLowerCase())) return entered;
  return `${entered}.${zone}`;
}

/** The inverse: the label a name carries under its zone.
 *
 * `NameModal` seeds its Name box from a stored row, and the box holds
 * the label rather than the FQDN — so it has to answer the same question
 * `composeHostname` does, backwards: *does this name already end with
 * its zone?*
 *
 * It lives here, beside the composer, because that question having two
 * homes is exactly the drift `HostnamesPage.test.tsx` sweeps for — and
 * the sweep found it, in `NameModal` with a `.${zone}` suffix while the
 * composer matched on the bare zone. Two spellings of one rule disagree
 * on the apex: `example.net` under zone `example.net` is the zone
 * itself, which the composer leaves alone and the modal's copy did not.
 *
 * Round-trips with `composeHostname`: `compose(decompose(n, z), z) === n`
 * for any `n` that ends with `z`.
 */
export function decomposeHostname(name: string, zone: string | null): string {
  if (zone === null || zone === '') return name;
  if (!name.toLowerCase().endsWith(zone.toLowerCase())) return name;
  if (name.length === zone.length) return name;
  // Strip the separating dot too, but only if that is what is there —
  // `notexample.net` ends with `example.net` and is not under it.
  const label = name.slice(0, name.length - zone.length);
  return label.endsWith('.') ? label.slice(0, -1) : name;
}
