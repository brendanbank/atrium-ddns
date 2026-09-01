/** #152 — the board's `!` must say WHICH joint diverged.
 *
 *  The two joints imply different actions: the upper one means DNS was
 *  changed somewhere other than here; the lower one means the device moved
 *  and the name has not followed. A marker saying "out of sync" for both
 *  would pass a weaker test and tell the operator nothing — so the third
 *  assertion is that the two texts DIFFER, not merely that a text exists.
 *
 *  Before the fix: 3 failed (the span was `aria-hidden` with no name).
 *  After:          3 passed.
 */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { BoardTable } from '../board/BoardTable';
import { DdnsRoot } from '../host/DdnsRoot';
import { board, device, hostname, strip } from './fixtures';

type Joint = 'agreed' | 'diverged';

function markerLabel(upper: Joint, lower: Joint): string {
  const b = board({
    devices: [
      device({
        id: 1,
        name: 'router',
        hostnames: [
          hostname({
            id: 7,
            name: 'a.example.test',
            strips: [strip({ upper_joint: upper, lower_joint: lower, collapsible: false })],
          }),
        ],
      }),
    ],
  });
  const { unmount } = render(
    <DdnsRoot>
      <BoardTable board={b} />
    </DdnsRoot>,
  );
  const label = screen.getByTestId('board-mark-a.example.test').getAttribute('aria-label') ?? '';
  unmount();
  return label;
}

describe('#152 — the marker explains itself', () => {
  it('names the upper joint when DNS disagrees with what we published', () => {
    expect(markerLabel('diverged', 'agreed')).toMatch(/DNS does not carry/i);
  });

  it('names the lower joint when the device has moved', () => {
    expect(markerLabel('agreed', 'diverged')).toMatch(/device is reporting a different address/i);
  });

  it('says something DIFFERENT for each — the whole point of the issue', () => {
    expect(markerLabel('diverged', 'agreed')).not.toBe(markerLabel('agreed', 'diverged'));
  });
});
