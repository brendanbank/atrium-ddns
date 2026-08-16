/** The resolution strip — the signature element, `docs/ops/ui-design.md` §3 and §4.
 *
 * Three rows on a rail, not three columns, and §3.1 is the measurement:
 * three columns needs ~1436px and atrium's `AppShell` leaves **1168px**
 * at a 1440px viewport, 1008px on a 1280px laptop. The failure mode of
 * the column layout is the worst available — a wrapped IPv6 address is
 * indistinguishable from a shorter one — so it is disqualified rather
 * than tight. This arrangement's minimum is ~592px.
 *
 * ## What is being read
 *
 * Not the three cells. The two **joints** between them:
 *
 * - **upper** — `dns_ip_*` vs `last_ip_*`: does the zone carry what we
 *   wrote?
 * - **lower** — `last_ip_*` vs `Device.last_ip_*`: has the device moved
 *   without the name following?
 *
 * The stations run upward against time (the device called from → we
 * published → the world answers), which makes this a chain of custody
 * and not a timeline. There is no time axis, the vertical spacing
 * carries no duration, and nothing may be added here that implies one.
 *
 * ## Both verdicts arrive decided
 *
 * This component contains no comparison. `upper_joint`, `lower_joint`,
 * the `; answered` status, the collapse counts and the reason the lower
 * joint was skipped are all fields on the payload. See `../api/board`
 * for why.
 */
import { useState, type ReactNode } from 'react';

import type { JointVerdict, Strip } from '../api/board';
import { AddressText } from './AddressText';
import {
  absoluteTitle,
  agreementSummary,
  answeredCell,
  calledFromLabel,
  formatStationTime,
  jointGlyph,
  jointWord,
  type Tone,
} from './format';

/** One segment of the rail plus its gutter glyph. `aria-hidden` on the
 *  drawing; the word travels with the station it belongs to. */
function RailJoint({ verdict }: { verdict: JointVerdict }) {
  const glyph = jointGlyph(verdict);
  return (
    <span
      className="ddns-rail__joint"
      data-verdict={verdict}
      data-testid={`rail-joint-${verdict}`}
    >
      <span className="ddns-rail__seg" />
      {glyph ? (
        <span className="ddns-rail__glyph" aria-hidden="true">
          {glyph}
        </span>
      ) : null}
      <span className="ddns-rail__seg" />
    </span>
  );
}

function Station({
  label,
  verdict,
  time,
  children,
  extra,
}: {
  label: string;
  /** The joint *below* this station, when there is one. Its word is
   *  announced here so a screen reader hears the verdict attached to the
   *  value it is about rather than to a decorative rule. */
  verdict?: JointVerdict;
  time: string | null;
  children: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div className="ddns-station">
      <span className="ddns-label">{label}</span>
      <span className="ddns-station__value">
        {children}
        {verdict ? (
          <span className="ddns-sr"> — {jointWord(verdict)}</span>
        ) : null}
      </span>
      <span className="ddns-station__time" title={absoluteTitle(time)}>
        {formatStationTime(time)}
      </span>
      {extra}
    </div>
  );
}

function Quiet({ children }: { children: ReactNode }) {
  return (
    <span className="ddns-data ddns-address" data-tone="quiet">
      {children}
    </span>
  );
}

export interface ResolutionStripProps {
  hostname: string;
  strip: Strip;
}

export function ResolutionStrip({ hostname, strip }: ResolutionStripProps) {
  // §3.4: a strip whose every applicable joint is `agreed` collapses to
  // one line. A strip with any other verdict is expanded and is **not
  // collapsed by any default** — which is why the initial state is
  // derived from `collapsible` (a backend field) rather than from a
  // preference. Expanding one is local state. There is deliberately no
  // "collapse all": the collapsed state is *defined* as "agrees", so a
  // control that let a diverged strip render in the agreed shape would
  // make the shape a lie.
  const [expanded, setExpanded] = useState(!strip.collapsible);

  const answered = answeredCell(strip.answered.status, strip.answered.address);
  const diverged =
    strip.upper_joint === 'diverged' || strip.lower_joint === 'diverged';

  if (strip.collapsible && !expanded) {
    return (
      <button
        type="button"
        className="ddns-strip--collapsed"
        onClick={() => setExpanded(true)}
        data-testid={`strip-collapsed-${hostname}-${strip.family}`}
        aria-expanded="false"
      >
        <span className="ddns-data">{hostname}</span>
        <span className="ddns-data ddns-strip__family">{strip.family}</span>
        <span className="ddns-label">{agreementSummary(strip)}</span>
        <AddressText value={strip.published.address} />
        <span
          className="ddns-station__time"
          title={absoluteTitle(strip.answered.checked_at)}
        >
          {formatStationTime(strip.answered.checked_at)}
        </span>
      </button>
    );
  }

  return (
    <article
      className="ddns-strip"
      data-diverged={diverged ? 'true' : 'false'}
      data-family={strip.family}
      data-testid={`strip-${hostname}-${strip.family}`}
    >
      <header className="ddns-strip__head">
        <span className="ddns-data">{hostname}</span>
        <span className="ddns-data ddns-strip__family">{strip.family}</span>
      </header>
      <div className="ddns-strip__body">
        <div className="ddns-rail" aria-hidden="true">
          <RailJoint verdict={strip.upper_joint} />
          <RailJoint verdict={strip.lower_joint} />
        </div>
        <div className="ddns-strip__stations">
          <Station
            label="answered"
            verdict={strip.upper_joint}
            time={strip.answered.checked_at}
            extra={
              strip.answered.error ? (
                <span className="ddns-strip__error">
                  {strip.answered.error}
                </span>
              ) : null
            }
          >
            {/* §4.2's five-row table. `n/a`, `unmeasured` and `no record`
                are three different strings for three different facts,
                and none of them is a bare dash — `0.0.0.0`, `::` and `-`
                are all ambiguous across the three null states. */}
            {answered.text === strip.answered.address &&
            strip.answered.address !== null ? (
              <AddressText
                value={strip.answered.address}
                compareTo={strip.published.address}
                tone={answered.tone}
                data-testid="answered-address"
              />
            ) : (
              <span
                className="ddns-data ddns-address"
                data-tone={answered.tone}
                data-testid="answered-address"
              >
                {answered.text}
              </span>
            )}
          </Station>

          <Station
            label="published"
            verdict={strip.lower_joint}
            time={strip.published.updated_at}
          >
            {strip.published.address === null ? (
              <Quiet>nothing published</Quiet>
            ) : (
              <AddressText
                value={strip.published.address}
                tone={publishedTone(strip)}
                data-testid="published-address"
              />
            )}
          </Station>

          <Station
            label={calledFromLabel(strip.called_from.reason)}
            time={strip.called_from.seen_at}
            extra={
              strip.called_from.declared_address ? (
                <span className="ddns-strip__error">
                  declares {strip.called_from.declared_address}
                </span>
              ) : null
            }
          >
            {strip.called_from.address === null ? (
              <Quiet>
                {strip.called_from.reason === 'no_device'
                  ? 'no device assigned'
                  : 'never called'}
              </Quiet>
            ) : (
              <AddressText
                value={strip.called_from.address}
                compareTo={
                  // Only underline against the published address when a
                  // comparison was actually made. Highlighting the
                  // differing groups of two values nobody compared is
                  // the same false positive the reason field exists to
                  // prevent, one channel down.
                  strip.lower_joint === 'diverged'
                    ? strip.published.address
                    : null
                }
                tone={strip.lower_joint === 'diverged' ? 'diverge' : 'ink'}
                data-testid="called-from-address"
              />
            )}
          </Station>
        </div>
      </div>
    </article>
  );
}

/** The published cell carries the accent when it is the *lower* cell of
 *  a diverged joint — §3.2's "lower cell in `--ddns-diverge`". The upper
 *  joint's lower cell is `; published`, so a diverged upper joint
 *  accents it too. */
function publishedTone(strip: Strip): Tone {
  return strip.upper_joint === 'diverged' ? 'diverge' : 'ink';
}

/** §4.5: a loading strip has **no rail**, so it cannot be mistaken for
 *  an agreed one. A static block, not a shimmer — this design spends its
 *  only transition on the rail (§3.7). */
export function StripSkeleton() {
  return <div className="ddns-strip--loading" data-testid="strip-loading" />;
}
