/** One event, in full.
 *
 * The ledger row carries the fields you scan; this carries the ones you
 * only want once you have found the row — `message`, the ids, the exact
 * timestamp, and the two addresses whose difference is the interesting
 * part of a NAT'd update.
 *
 * Why a modal rather than a second line per row: the second line was
 * unconditional, so every row in a 200-row page paid for a detail that
 * matters on the few you actually stop at. It also made `called from`
 * and `declared myip` look like columns while lining up with nothing.
 */
import { Modal, Stack, Text } from '@mantine/core';

import type { EventRow } from '../api/events';
import { CARD_MODAL_PROPS, CARD_MODAL_STYLES } from '../cards';
import { DdnsPortalScope } from '../host/DdnsRoot';
import { absoluteTitle } from '../board/format';

/** One label/value pair. `null` is rendered, not skipped: "this event
 *  has no backend" and "this field was not in the response" are
 *  different facts, and a row that silently disappears makes them look
 *  the same. */
function Field({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="ddns-logdetail__row">
      <span className="ddns-th">{label}</span>
      <span className="ddns-cell" data-testid={`log-detail-${label}`}>
        {value ?? '—'}
      </span>
    </div>
  );
}

export function LogDetail({
  row,
  onClose,
}: {
  row: EventRow | null;
  onClose: () => void;
}) {
  return (
    <Modal
      opened={row !== null}
      onClose={onClose}
      title="Event"
      size={640}
      {...CARD_MODAL_PROPS}
      styles={CARD_MODAL_STYLES}
      data-testid="log-detail-modal"
    >
      {/* Portalled outside `[data-ddns-root]`, so without this the whole
          dialog renders with none of `ddns.css`. */}
      <DdnsPortalScope>
        {row ? (
          <Stack gap="xs" data-testid="log-detail">
            <div className="ddns-logdetail">
              <Field label="When" value={absoluteTitle(row.created_at)} />
              <Field label="Event" value={row.event_type} />
              <Field label="Result" value={row.response_code} />
              <Field label="Via" value={row.backend_type} />
              <Field label="Device" value={row.device_name} />
              <Field label="Name" value={row.hostname} />
              <Field label="Zone" value={row.domain_name} />
              <Field label="User" value={row.user_email} />
              <Field label="Called from" value={row.client_ip} />
              {/* The address the request was *about*. Shown always here,
                  unlike in the ledger where it appears only when it
                  differs — in a detail view "same as called from" is
                  itself worth being able to read. */}
              <Field label="Declared myip" value={row.ip} />
              <Field label="Event id" value={String(row.id)} />
            </div>
            {row.message ? (
              <Stack gap={2}>
                <span className="ddns-th">Message</span>
                {/* The server's own words, unwrapped and unreworded. */}
                <Text size="sm" ff="monospace" data-testid="log-detail-message">
                  {row.message}
                </Text>
              </Stack>
            ) : null}
          </Stack>
        ) : null}
      </DdnsPortalScope>
    </Modal>
  );
}
