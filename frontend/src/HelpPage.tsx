/** The help entry — #75, ui-parity §3.3 G5.
 *
 * The legacy `GET /admin/help` rendered `help.html`. #47's sweep looked
 * for a replacement across **both** deployed bundles, seven spellings
 * each, and found zero help surfaces anywhere — host side or shell side.
 * This is the cheap thing the issue asked for: one registered route and
 * one nav item, pointing at the operator documentation that already
 * exists in `docs/`.
 *
 * ## Two decisions worth reading before editing this file
 *
 * **The documentation is linked, not embedded.** `docs/` is not copied
 * into the image — the Dockerfile copies `backend/` and the built
 * bundle and nothing else — so a page that read a Markdown file at run
 * time would work in a checkout and 404 in the container. That is the
 * *"a file read at runtime must be copied into the image"* trap in
 * `docs/ops/overnight-template.md`, and the honest way round it is a
 * link to where the file actually lives.
 *
 * **The surface list is derived, not typed.** Every row below imports
 * the path constant the registration uses, so a page that moves takes
 * its help entry with it and a page that is deleted breaks the build
 * rather than leaving a link to nowhere. A hand-kept list is the
 * identical defect one release later; `main.test.tsx` closes the other
 * direction by asserting that every route this bundle registers appears
 * here.
 *
 * ## What this page is *not*
 *
 * It is not an operator handbook. The documents it links are design and
 * operations notes written for the people building this service, and
 * saying so is better than implying otherwise — see the issue comment on
 * #75 for the argument that a genuine handbook is separate work.
 */
import { Anchor, List, Stack, Text, Title } from '@mantine/core';

import { BOARD_PATH, DEVICES_PATH, DOMAINS_PATH } from './paths';
import { NAMES_PATH } from './HostnamesPage';
import { LOG_PATH } from './LogSearchPage';
import { DdnsRoot } from './host/DdnsRoot';

/** Where this bundle's own path lives. Exported for the same reason
 *  every other page exports one: the route and the nav item must be one
 *  string, not two that drift. */
export const HELP_PATH = '/atrium-ddns/help';

/** The repository the linked documents live in.
 *
 * A URL rather than a relative path because these files are not served
 * by this application at all. Deliberately the repository root and a
 * path under it, so a reader who ends up on a fork or a private mirror
 * can see immediately which repository is meant.
 */
export const DOCS_BASE =
  'https://github.com/brendanbank/atrium-ddns/blob/master';

/** One row per document. `path` is what a reader would `open` in a
 *  checkout; the anchor points at the same file on the forge. */
export const DOCUMENTS: { path: string; title: string; blurb: string }[] = [
  {
    path: 'README.md',
    title: 'README',
    blurb:
      'Standing the stack up, the make targets, and what lives where. The first thing to read on a new box.',
  },
  {
    path: 'docs/ops/ui-design.md',
    title: 'Interface design notes',
    blurb:
      'What the device board is showing and why: the five DNS states, the four device states, the resolution strip and the rule that none of them is recomputed in the browser.',
  },
  {
    path: 'docs/ops/refactor-plan.md',
    title: 'Design and migration plan',
    blurb:
      'The model — zones, devices, names, provider bindings — and the decisions behind it, including why a device is the DDNS credential.',
  },
  {
    path: 'docs/ops/ui-parity.md',
    title: 'Legacy route parity',
    blurb:
      'Every route the old service had, and what covers it now. Read this before concluding something is missing.',
  },
  {
    path: 'docs/ops/overnight-template.md',
    title: 'Operations contract',
    blurb:
      'How changes reach this deployment: the gate, the evidence rules, and the deploy procedure.',
  },
];

/** One row per surface this bundle registers. The path comes from the
 *  registration's own constant — see the module docstring. */
export const SURFACES: { to: string; label: string; blurb: string }[] = [
  {
    to: BOARD_PATH,
    label: 'Devices and names',
    blurb:
      'The board. Which router has gone quiet, and whether what DNS answers matches what we last published. “Check now” re-resolves everything you can see instead of waiting for the scheduled check.',
  },
  {
    to: NAMES_PATH,
    label: 'Names',
    blurb:
      'Register a hostname under one of your zones and assign it to a device. A name is refused here if the wire protocol could never update it.',
  },
  {
    to: DOMAINS_PATH,
    label: 'Zones and providers',
    blurb:
      'Claim a zone, bind a DNS provider to it, and store that provider’s credential. Renaming a zone is refused if it would leave a name outside it.',
  },
  {
    to: DEVICES_PATH,
    label: 'Devices',
    blurb:
      'The credential a router authenticates with. The secret is shown once, at creation and at rotation, and cannot be displayed again.',
  },
  {
    to: LOG_PATH,
    label: 'Log search',
    blurb:
      'Every update, delete and authentication attempt, filterable by device, zone, name and address.',
  },
];

export function HelpInner() {
  return (
    <Stack gap="md">
      <Title order={3}>Help</Title>

      <Text size="sm">
        This service publishes DNS records for routers that call it when
        their address changes. A <strong>zone</strong> is a domain you
        control, a <strong>device</strong> is the credential one router
        uses, and a <strong>name</strong> is a hostname inside a zone
        that a device keeps up to date.
      </Text>

      <Title order={5}>The pages</Title>
      <List spacing="xs" size="sm" data-testid="help-surfaces">
        {SURFACES.map((surface) => (
          <List.Item key={surface.to}>
            {/* A plain anchor, for the reason `LogLink` gives: this tree
                is mounted inside atrium's React, so react-router's
                `Link` is not reachable and a bare `pushState` would move
                the address bar without telling the router. */}
            <Anchor href={surface.to} data-testid={`help-to-${surface.to}`}>
              {surface.label}
            </Anchor>{' '}
            — {surface.blurb}
          </List.Item>
        ))}
      </List>

      <Title order={5}>Documentation</Title>
      <Text size="xs" c="dimmed" data-testid="help-docs-caveat">
        These are the project’s own design and operations notes, written
        for the people who build and run this service. They are hosted on
        the repository rather than served here: they are not copied into
        the deployed image, and a page that read them at run time would
        work in a checkout and fail in the container.
      </Text>
      <List spacing="xs" size="sm" data-testid="help-documents">
        {DOCUMENTS.map((doc) => (
          <List.Item key={doc.path}>
            <Anchor
              href={`${DOCS_BASE}/${doc.path}`}
              target="_blank"
              rel="noreferrer"
              data-testid={`help-doc-${doc.path}`}
            >
              {doc.title}
            </Anchor>{' '}
            <code>{doc.path}</code> — {doc.blurb}
          </List.Item>
        ))}
      </List>

      <Title order={5}>Pointing a router at this service</Title>
      <Text size="sm" data-testid="help-wire">
        Routers use the same protocol the old service spoke. Configure
        the device’s username and secret as HTTP Basic credentials
        against <code>/nic/update</code>; the reply is one line —{' '}
        <code>good</code>, <code>nochg</code>, <code>nohost</code>,{' '}
        <code>badauth</code>, <code>notfqdn</code>, <code>abuse</code>,{' '}
        <code>dnserr</code> or <code>911</code>. Every one of those
        replies is written to the log, so “Log search” is where to look
        when a router says it updated and the address did not move.
      </Text>
    </Stack>
  );
}

export function HelpPage() {
  return (
    <DdnsRoot>
      <HelpInner />
    </DdnsRoot>
  );
}
