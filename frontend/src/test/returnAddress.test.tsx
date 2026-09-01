/** Where a create flow started from the board comes back to.
 *
 * The board is the landing surface and the only nav entry, so a create that
 * navigates to `/atrium-ddns/devices` and finishes there leaves you on a page
 * with no way back. `?from=` carries the return address.
 *
 * This file exists because the bug it covers was reported twice by hand. The
 * first fix asserted that `targetFromSearch` *parsed* `?device=new` and called
 * the flow verified — but nothing checked the **consumer**, and the consumer
 * did nothing with it. Parsing an address is not answering it.
 */
import { describe, expect, test } from 'vitest';

import {
  boardDeviceHref,
  boardNameHref,
  boardNameNewHref,
  returnFromSearch,
  withReturn,
} from '../paths';

describe('the return address', () => {
  test('the board composes one onto both create hrefs', () => {
    // Opening a name from inside a device card carries the card's own
    // address, so closing the name goes back to the device rather than to
    // the bare board. This is the whole return mechanism: no stack, no
    // component state, just the address — so it survives a reload.
    expect(withReturn(boardNameHref(3), boardDeviceHref(7))).toBe(
      '/atrium-ddns?name=3&from=%2Fatrium-ddns%3Fdevice%3D7',
    );
  });

  test('it round-trips through the search string', () => {
    const href = withReturn(boardNameHref(3), boardDeviceHref(7));
    expect(returnFromSearch(href.slice(href.indexOf('?')))).toBe(
      '/atrium-ddns?device=7',
    );
  });

  test('absent is null, so the flow lands on its own list as before', () => {
    expect(returnFromSearch('?name=new')).toBeNull();
    expect(returnFromSearch('')).toBeNull();
  });

  test('it refuses anything outside this bundle', () => {
    // A return address is a redirect target read from the URL. Unchecked, a
    // pasted link sends the user wherever it says — including off-site.
    // `//evil.example` is the one that looks like a path and is not.
    expect(returnFromSearch('?from=%2F%2Fevil.example')).toBeNull();
    expect(returnFromSearch('?from=https%3A%2F%2Fevil.example')).toBeNull();
    expect(returnFromSearch('?from=%2Fadmin')).toBeNull();
    expect(returnFromSearch('?from=')).toBeNull();
  });
});

describe('the per-row add-a-name link', () => {
  test('names the device the row is about', () => {
    // The board's row `+` knows which device it belongs to. Making the
    // operator pick it again from a dropdown they just came from is the
    // gap that gets filled with the wrong answer.
    expect(boardNameNewHref(7)).toBe('/atrium-ddns?name=new&for=7');
  });

  test('omitted, the name starts unassigned — the header link', () => {
    expect(boardNameNewHref()).toBe('/atrium-ddns?name=new');
  });

  test('the return address composes onto it either way', () => {
    expect(withReturn(boardNameNewHref(7), boardDeviceHref(7))).toBe(
      '/atrium-ddns?name=new&for=7&from=%2Fatrium-ddns%3Fdevice%3D7',
    );
  });
});
