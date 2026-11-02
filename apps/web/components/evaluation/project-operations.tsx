"use client";

import { useEffect, useState } from "react";
import { ClipboardCopy, GitPullRequest, Plus, RefreshCw, Trash2 } from "lucide-react";
import { platform } from "@/lib/platform-api";
import { initialValue, SchemaForm, schemaDefinitions } from "./schema-form";
import type { IntegrationIn, VersionRecord } from "@/types/platform-generated";

type Integration = {
  id: string;
  provider: string;
  repository: string | null;
  revoked_at: string | null;
  configuration: IntegrationIn;
};

export function ProjectOperations({
  projectId,
  owner,
  versions,
}: {
  projectId: string;
  owner: boolean;
  versions: VersionRecord[];
}) {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [draft, setDraft] = useState<unknown>(
    initialValue(schemaDefinitions.IntegrationIn),
  );
  const [records, setRecords] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    if (owner)
      setIntegrations(
        (
          await platform<{ items: Integration[] }>(
            `/projects/${projectId}/integrations`,
          )
        ).items,
      );
  }
  useEffect(() => {
    let active = true;
    if (owner)
      platform<{ items: Integration[] }>(`/projects/${projectId}/integrations`)
        .then((data) => {
          if (active) setIntegrations(data.items);
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [projectId, owner]);
  async function act(operation: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await operation();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {error && (
        <div className="ev-error" role="alert">
          {error}
        </div>
      )}
      {owner && (
        <>
          <h3>Integrations</h3>
          {integrations.length ? (
            <div className="ev-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Provider</th>
                    <th>Repository</th>
                    <th>Permission</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {integrations.map((row) => (
                    <tr key={row.id}>
                      <td>{row.provider}</td>
                      <td>{row.repository ?? row.id}
                        {row.configuration.relay && <button className="ev-icon" type="button"
                          title="Copy task callback URL" aria-label="Copy task callback URL"
                          onClick={() => void navigator.clipboard.writeText(
                            `${row.configuration.relay?.public_base_url}/api/v1/callbacks/runtime/${row.id}`
                          )}><ClipboardCopy size={15} /></button>}
                      </td>
                      <td>
                        {row.configuration.publish_enabled
                          ? "Draft PR publication"
                          : "Read / relay only"}
                      </td>
                      <td>{row.revoked_at ? "Revoked" : "Enabled"}</td>
                      <td>
                        <button
                          className="ev-icon"
                          title="Revoke integration"
                          aria-label={`Revoke ${row.id}`}
                          disabled={busy || !!row.revoked_at}
                          onClick={() =>
                            void act(() =>
                              platform(`/integrations/${row.id}`, "DELETE"),
                            )
                          }
                        >
                          <Trash2 size={16} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="ev-muted">No integrations configured.</p>
          )}
          <form
            className="ev-form"
            onSubmit={(event) => {
              event.preventDefault();
              void act(() =>
                platform(`/projects/${projectId}/integrations`, "POST", draft),
              );
            }}
          >
            <SchemaForm
              name="Integration"
              schema={schemaDefinitions.IntegrationIn}
              value={draft}
              onChange={setDraft}
              versions={versions}
            />
            <div className="ev-form-actions">
              <button className="ev-secondary" disabled={busy}>
                <Plus size={16} />
                Add Integration
              </button>
            </div>
          </form>
          <h3>Operations</h3>
          <div className="ev-toolbar">
            {[
              "health",
              "audit",
              "internal-jobs",
              "dependencies",
              "invalidations",
            ].map((path) => (
              <button
                key={path}
                className="ev-secondary"
                disabled={busy}
                onClick={() =>
                  void act(async () =>
                    setRecords(
                      await platform(`/projects/${projectId}/${path}`),
                    ),
                  )
                }
              >
                <RefreshCw size={14} />
                {path.replaceAll("-", " ")}
              </button>
            ))}
          </div>
          {records !== null && (
            <pre className="ev-details">{JSON.stringify(records, null, 2)}</pre>
          )}
        </>
      )}
    </>
  );
}

export function PublishRepair({
  repairId,
  projectId,
  proposalHash,
  enabled,
  onPublished,
}: {
  repairId: string;
  projectId: string;
  proposalHash: string;
  enabled: boolean;
  onPublished: () => Promise<void>;
}) {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    platform<{ items: Integration[] }>(
      `/projects/${projectId}/publication-targets`,
    )
      .then((data) => {
        if (active) setIntegrations(data.items);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [projectId, enabled]);
  if (!enabled) return null;
  return (
    <form
      className="ev-form"
      onSubmit={async (event) => {
        event.preventDefault();
        const values = new FormData(event.currentTarget);
        if (
          !window.confirm(
            "Create an authorized draft pull request for this exact patch?",
          )
        )
          return;
        setBusy(true);
        setError("");
        try {
          await platform(`/repairs/${repairId}/publish`, "POST", {
            integration_id: values.get("integration"),
            proposal_hash: proposalHash,
          });
          await onPublished();
        } catch (e) {
          setError((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}
    >
      {error && (
        <div className="ev-error" role="alert">
          {error}
        </div>
      )}
      <label>
        Publication Target
        <select name="integration" required>
          <option value="">Select authorized repository</option>
          {integrations.map((row) => (
            <option value={row.id} key={row.id}>
              {row.repository}
            </option>
          ))}
        </select>
      </label>
      <div className="ev-form-actions">
        <button className="ev-primary" disabled={busy || !integrations.length}>
          <GitPullRequest size={16} />
          Authorize Draft PR
        </button>
      </div>
    </form>
  );
}
