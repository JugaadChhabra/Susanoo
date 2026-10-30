"use client";

import { useEffect, useState } from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { platform } from "@/lib/platform-api";

type Resource = {
  id: string;
  resource_key: string;
  attempt_id: string;
  state: string;
  fencing_generation: number;
};
type Reconciliation = {
  id: string;
  state: string;
  rationale: string;
  evidence_ids: string[];
};
export function ResourceQuarantine({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<Resource[]>([]);
  const [selected, setSelected] = useState<Resource | null>(null);
  const [details, setDetails] = useState<{
    items: Reconciliation[];
    evidence: unknown[];
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    platform<{ items: Resource[] }>(`/projects/${projectId}/resource-leases`)
      .then((data) => {
        if (active) setRows(data.items);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [projectId]);
  return (
    <section>
      <h3>Resource Quarantine</h3>
      {error && (
        <div className="ev-error" role="alert">
          {error}
        </div>
      )}
      <button
        className="ev-icon"
        title="Refresh resource leases"
        aria-label="Refresh resource leases"
        onClick={async () => {
          try {
            setRows(
              (
                await platform<{ items: Resource[] }>(
                  `/projects/${projectId}/resource-leases`,
                )
              ).items,
            );
          } catch (e) {
            setError((e as Error).message);
          }
        }}
      >
        <RefreshCw size={16} />
      </button>
      {rows.length ? (
        <div className="ev-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Resource</th>
                <th>State</th>
                <th>Generation</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.resource_key}</td>
                  <td>{row.state}</td>
                  <td>{row.fencing_generation}</td>
                  <td>
                    <button
                      className="ev-secondary"
                      onClick={async () => {
                        try {
                          setDetails(
                            await platform(
                              `/attempts/${row.attempt_id}/reconciliations`,
                            ),
                          );
                          setSelected(row);
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    >
                      Inspect
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="ev-muted">No exclusive resource leases.</p>
      )}
      {selected && details && (
        <form
          className="ev-form"
          onSubmit={async (event) => {
            event.preventDefault();
            const values = new FormData(event.currentTarget);
            setBusy(true);
            setError("");
            try {
              await platform(
                `/resource-leases/${selected.id}/release`,
                "POST",
                {
                  reconciliation_id: values.get("reconciliation"),
                  rationale: values.get("rationale"),
                },
              );
              setRows(
                (
                  await platform<{ items: Resource[] }>(
                    `/projects/${projectId}/resource-leases`,
                  )
                ).items,
              );
              setSelected(null);
              setDetails(null);
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            Resolved Reconciliation
            <select name="reconciliation" required>
              <option value="">Select evidence-bearing read-back</option>
              {details.items
                .filter(
                  (item) =>
                    item.state === "resolved" && item.evidence_ids.length,
                )
                .map((item) => (
                  <option value={item.id} key={item.id}>
                    {item.rationale}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Release Rationale
            <textarea name="rationale" required maxLength={4000} />
          </label>
          <details className="ev-details">
            <summary>Read-Back Evidence</summary>
            <pre>{JSON.stringify(details, null, 2)}</pre>
          </details>
          <div className="ev-form-actions">
            <button
              className="ev-secondary"
              disabled={busy || selected.state !== "quarantined"}
            >
              <ShieldCheck size={16} />
              Release Verified Resource
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
