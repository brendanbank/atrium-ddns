/** The card modal — its width, and the one click it may intercept.
 *
 * `docs/ops/ui-design.md` **Part III §17**. The operator asked twice for
 * "a modal that pops up", and that overrules §12. This module holds the
 * two facts that decision needs and that must not be retyped at a call
 * site.
 *
 * ## Why the width is computed and not chosen
 *
 * §12 rejected a right drawer at Mantine's `lg` — 620px — because one
 * resolution strip needs **≈592px** (§3.1) and the drawer's own padding
 * takes it below that: the signature element would wrap *inside its own
 * detail view*. §17 records that this was never an argument against a
 * modal, whose `size` is arbitrary, and then attaches the condition:
 *
 * > The modal must be sized to hold a strip at the width §3.1 measured.
 * > A modal that wraps the signature element reintroduces exactly the
 * > failure §12 was written to avoid, and would be the worst of both
 * > decisions.
 *
 * So the width is derived from the measurement rather than picked and
 * eyeballed, and `frontend/tests-e2e/card-affordance.spec.ts` measures
 * the rendered body in a browser rather than trusting the arithmetic.
 *
 * Two minima, and the larger one wins. §3.1 budgets 110px for the label
 * column and arrives at 592px. `ddns.css` §3.3's recorded deviation then
 * measures the *qualified* labels — `; called from (no update on
 * record)` is 35 characters, 246px in the data face — and the same strip
 * needs **≈728px** when one of those is on screen. A modal sized to 592
 * would hold the common case and wrap the qualified one, which is the
 * same defect arriving later and harder to see.
 */

/** §3.1's measured minimum for one resolution strip. */
export const ONE_STRIP_MIN_PX = 592;

/** The same strip when a station carries a qualified label — the
 *  deviation recorded in `ddns.css` §3.3, found by looking at a
 *  screenshot rather than by reading the design. */
export const QUALIFIED_STRIP_MIN_PX = 728;

/** Mantine's `md` spacing, which is what `<Modal padding="md">` puts on
 *  each side of the body. Named here because it is what turns a content
 *  minimum into an outer width. */
export const CARD_MODAL_PADDING_PX = 16;

/** The `size` the card modals are opened at.
 *
 * `Modal`'s `size` lands on the border box, and the body's padding is
 * inside it, so the content a strip actually gets is
 * `CARD_MODAL_WIDTH_PX - 2 * CARD_MODAL_PADDING_PX` = the qualified
 * minimum above. Derived, so moving either measurement moves this.
 */
export const CARD_MODAL_WIDTH_PX =
  QUALIFIED_STRIP_MIN_PX + 2 * CARD_MODAL_PADDING_PX;

/**
 * Whether a click on a link may be turned into a modal.
 *
 * The rows keep real `<a href>`s — §17 keeps the routes, and an operator
 * pasting a zone URL into a ticket is the case that argument names, so
 * the destination has to survive copy-link, middle-click and
 * cmd/ctrl-click. Only the plain left click is intercepted; every
 * modified click falls through to the browser and opens the route,
 * which is exactly what the reader who used a modifier asked for.
 *
 * `event.button === 0` excludes the middle click, which arrives as a
 * `click` in some browsers and is the one people use to open a row in a
 * background tab.
 */
export function opensInThisTab(event: {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}): boolean {
  return (
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}
