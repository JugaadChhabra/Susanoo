"use client";

import { useEffect, useState } from "react";
import { ClipboardCopy, KeyRound, RefreshCw, Trash2, X } from "lucide-react";
import { platform } from "@/lib/platform-api";

type Decision = { id: string; trigger_kind: string; trigger_key: string; version_key: string;
  allowed: boolean; reasons: string[]; delayed_until: string | null; run_id: string | null; created_at: string };
type Credential = { id: string; expires_at: string; revoked_at: string | null };
type Task = { id: string; attempt_id: string; terminal: boolean; last_sequence: number; expires_at: string };
type Trace = { record_id: string; created_at: string; payload: { conversation_id: string; evidence_class: string;
  turns: { index: number; role: string; text: string | null; status: string }[];
  provenance: { source_kind: string; completeness: string; redactions: number; missing: string[] } } };

export function RuntimeOperations({ projectId, owner }: { projectId: string; owner: boolean }) {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [cursor, setCursor] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function refresh() {
    setBusy(true); setError("");
    try {
      const [automatic, passive, keys, relay] = await Promise.all([
        platform<{ items: Decision[] }>(`/projects/${projectId}/automatic-decisions`),
        platform<{ items: Trace[]; next_cursor: string }>(`/projects/${projectId}/passive-records`),
        owner ? platform<{ items: Credential[] }>(`/projects/${projectId}/collector-credentials`) : { items: [] },
        owner ? platform<{ items: Task[] }>(`/projects/${projectId}/relay-tasks`) : { items: [] },
      ]);
      setDecisions(automatic.items); setTraces(passive.items); setCursor(passive.next_cursor);
      setCredentials(keys.items); setTasks(relay.items);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  useEffect(() => {
    let active = true;
    Promise.all([
      platform<{ items: Decision[] }>(`/projects/${projectId}/automatic-decisions`),
      platform<{ items: Trace[]; next_cursor: string }>(`/projects/${projectId}/passive-records`),
      owner ? platform<{ items: Credential[] }>(`/projects/${projectId}/collector-credentials`) : { items: [] },
      owner ? platform<{ items: Task[] }>(`/projects/${projectId}/relay-tasks`) : { items: [] },
    ]).then(([automatic, passive, keys, relay]) => {
      if (active) { setDecisions(automatic.items); setTraces(passive.items); setCursor(passive.next_cursor);
        setCredentials(keys.items); setTasks(relay.items); }
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId, owner]);
  return <section className="ev-report-section" aria-label="Automation and passive evidence">
    <div className="ev-inline"><h3>Automatic Run Decisions</h3>
      <button className="ev-icon" title="Refresh runtime operations" aria-label="Refresh runtime operations"
        disabled={busy} onClick={() => void refresh()}><RefreshCw size={16} /></button></div>
    {error && <div className="ev-error" role="alert">{error}</div>}
    {decisions.length ? <div className="ev-table-wrap"><table><thead><tr><th>Trigger</th><th>Decision</th>
      <th>Reasons</th><th>Delayed Until</th><th>Run</th><th>Recorded</th></tr></thead>
      <tbody>{decisions.map((row) => <tr key={row.id}><td>{row.trigger_kind}<br /><small>{row.trigger_key}</small></td>
        <td>{row.allowed ? "Admitted" : "Blocked"}</td><td>{row.reasons.length ? row.reasons.join("; ") : "Within policy"}</td>
        <td>{row.delayed_until ? new Date(row.delayed_until).toLocaleString() : "None"}</td>
        <td>{row.run_id ?? "Not started"}</td><td>{new Date(row.created_at).toLocaleString()}</td></tr>)}</tbody>
    </table></div> : <div className="ev-empty">No automatic run decisions.</div>}
    <dl className="ev-report-facts"><div><dt>External agent cost estimate</dt><dd>Unknown</dd></div></dl>
    <h3>Passive Evidence</h3>
    {owner && <>
      <div className="ev-toolbar"><button className="ev-secondary" disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try { const value = await platform<{ token: string }>(`/projects/${projectId}/collector-credentials`, "POST", {});
          setToken(value.token); await refresh(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
      }}><KeyRound size={16} />Issue Collector Credential</button></div>
      {token && <div className="ev-token"><div className="ev-inline"><strong>New Collector Credential</strong>
        <button className="ev-icon" title="Copy collector credential" aria-label="Copy collector credential"
          onClick={() => void navigator.clipboard.writeText(token)}><ClipboardCopy size={16} /></button>
        <button className="ev-icon" title="Hide collector credential" aria-label="Hide collector credential"
          onClick={() => setToken("")}><X size={16} /></button></div><code>{token}</code></div>}
      {credentials.length > 0 && <div className="ev-table-wrap"><table><thead><tr><th>Credential</th><th>Expires</th>
        <th>Status</th><th /></tr></thead><tbody>{credentials.map((row) => <tr key={row.id}><td>{row.id}</td>
        <td>{new Date(row.expires_at).toLocaleString()}</td><td>{row.revoked_at ? "Revoked" : "Issued"}</td>
        <td><button className="ev-icon" title="Revoke collector credential" aria-label="Revoke collector credential"
          disabled={busy || Boolean(row.revoked_at)} onClick={async () => {
            setBusy(true); setError(""); try { await platform(`/credentials/${row.id}/revoke`, "POST"); await refresh(); }
            catch (e) { setError((e as Error).message); } finally { setBusy(false); }
          }}><Trash2 size={15} /></button></td></tr>)}</tbody></table></div>}
    </>}
    {traces.length ? <>{traces.map((row) => <details key={row.record_id}><summary>
      {row.payload.provenance.source_kind} / {row.payload.provenance.completeness} / {row.record_id.slice(0, 12)}
    </summary><dl className="ev-report-facts"><div><dt>Evidence class</dt><dd>{row.payload.evidence_class}</dd></div>
      <div><dt>Backend outcome verification</dt><dd>Not established</dd></div>
      <div><dt>Redactions</dt><dd>{row.payload.provenance.redactions}</dd></div>
      <div><dt>Missing evidence</dt><dd>{row.payload.provenance.missing.join(", ") || "None reported"}</dd></div></dl>
      {row.payload.turns.map((turn) => <div key={turn.index}><strong>{turn.role} / {turn.status}</strong><p>{turn.text}</p></div>)}
    </details>)}<button className="ev-secondary" disabled={busy || !cursor} onClick={async () => {
      setBusy(true); setError(""); try {
        const page = await platform<{ items: Trace[]; next_cursor: string }>(
          `/projects/${projectId}/passive-records?after=${encodeURIComponent(cursor)}`);
        setTraces((old) => [...old, ...page.items]); setCursor(page.items.length ? page.next_cursor : "");
      } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
    }}>Load More Evidence</button></> : <div className="ev-empty">No passive evidence received.</div>}
    {owner && <><h3>Callback Tasks</h3>{tasks.length ? <div className="ev-table-wrap"><table><thead><tr>
      <th>Registry ID</th><th>Attempt</th><th>Terminal</th><th>Last Sequence</th><th>Expires</th></tr></thead><tbody>
      {tasks.map((row) => <tr key={row.id}><td>{row.id}</td><td>{row.attempt_id}</td><td>{row.terminal ? "Yes" : "No"}</td>
        <td>{row.last_sequence}</td><td>{new Date(row.expires_at).toLocaleString()}</td></tr>)}
    </tbody></table></div> : <div className="ev-empty">No registered callback tasks.</div>}</>}
  </section>;
}
