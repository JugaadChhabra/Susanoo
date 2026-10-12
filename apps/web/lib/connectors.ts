import type { ConnectionSummary, ConnectorInfo, ConnectorSpec } from "@/types/generated";

/** [PRD › Connectors and API budget] Plain-English helpers for the connectors screen and the connector picker. */

const STATUS_LABELS: Record<ConnectionSummary["status"], string> = {
  connected: "Connected",
  untested: "Not tested yet",
  failed: "Not working",
};

export const connectionStatusLabel = (status: ConnectionSummary["status"]) => STATUS_LABELS[status];

/** "GitHub · acme/payments-api": the connector plus whatever non-secret setting says what it reaches. */
export function connectionLabel(spec: Pick<ConnectorSpec, "name">, connection: ConnectionSummary): string {
  const reach = Object.values(connection.settings).filter(Boolean).join(", ");
  return reach ? `${spec.name} · ${reach}` : spec.name;
}

/** The user's saved connections, each with the connector it belongs to, ready for a picker. */
export function savedConnections(connectors: ConnectorInfo[]): (ConnectorInfo & { connection: ConnectionSummary })[] {
  return connectors.filter((c): c is ConnectorInfo & { connection: ConnectionSummary } => c.connection !== null);
}

/** A project's connections by name, in the order the API lists them; ids of revoked connections are skipped. */
export function connectionNames(ids: string[], connectors: ConnectorInfo[]): { id: string; name: string }[] {
  const byId = new Map(savedConnections(connectors).map(({ spec, connection }) => [connection.id, connectionLabel(spec, connection)]));
  return ids.flatMap((id) => {
    const name = byId.get(id);
    return name === undefined ? [] : [{ id, name }];
  });
}

export const writesLabel = (connection: ConnectionSummary) =>
  connection.writes_enabled
    ? "Your agent can post comments."
    : "Dry run: your agent only tells you what it would have posted.";

/** A plain-English hint for the first empty field, or null when everything is filled in. */
export function missingField(spec: ConnectorSpec, values: Record<string, string>): string | null {
  const empty = spec.fields.find((f) => !(values[f.name] ?? "").trim());
  return empty ? `Fill in ${empty.label}.` : null;
}

export type GuideSegment =
  | { kind: "text"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "link"; text: string; href: string };

const INLINE = /\*\*(.+?)\*\*|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;

/** Guide steps use **bold** for UI labels and [text](https://…) links; everything else is plain text. */
export function guideSegments(step: string): GuideSegment[] {
  const out: GuideSegment[] = [];
  let last = 0;
  for (const match of step.matchAll(INLINE)) {
    if (match.index > last) out.push({ kind: "text", text: step.slice(last, match.index) });
    if (match[1] !== undefined) out.push({ kind: "bold", text: match[1] });
    else out.push({ kind: "link", text: match[2], href: match[3] });
    last = match.index + match[0].length;
  }
  if (last < step.length) out.push({ kind: "text", text: step.slice(last) });
  return out;
}
