"use client";

import { connectionLabel, connectionStatusLabel } from "@/lib/connectors";
import type { ConnectionSummary, ConnectorInfo } from "@/types/generated";

/** Tick which of the user's saved connections a project's agent may use. [PRD › Connectors and API budget] */
export function ConnectionCheckboxes({
  connected,
  picked,
  onChange,
}: {
  connected: (ConnectorInfo & { connection: ConnectionSummary })[];
  picked: string[];
  onChange: (ids: string[]) => void;
}) {
  return (
    <div className="space-y-2">
      {connected.map(({ spec, connection }) => (
        <label
          key={connection.id}
          className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm"
        >
          <input
            type="checkbox"
            className="mt-0.5 size-4"
            checked={picked.includes(connection.id)}
            onChange={(e) =>
              onChange(e.target.checked ? [...picked, connection.id] : picked.filter((id) => id !== connection.id))
            }
          />
          <span className="min-w-0">
            <span className="font-medium break-words">{connectionLabel(spec, connection)}</span>
            <span className="block text-muted-foreground">
              {connectionStatusLabel(connection.status)} · {spec.summary}
            </span>
          </span>
        </label>
      ))}
    </div>
  );
}
