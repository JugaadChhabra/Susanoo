"use client";

import { useEffect, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { platform } from "@/lib/platform-api";
import type {
  ArtifactRecord,
  CheckResult,
  Observation,
} from "@/types/platform-generated";

type Evidence = {
  id: string;
  kind: "observation" | "check";
  attempt_id: string;
  content_hash: string;
  view_hash: string;
  data: Observation | CheckResult;
};
type EvidencePage = {
  items: Evidence[];
  next_cursor: string | null;
  retention: { reason: string } | null;
};
type EventsPage = {
  items: {
    event_id: string;
    sequence: number;
    event_type: string;
    attempt_id: string;
    payload: unknown;
  }[];
  next_after: number;
};

function EvidenceArtifact({ id }: { id: string }) {
  const [record, setRecord] = useState<ArtifactRecord | null>(null);
  const [error, setError] = useState("");
  return (
    <div className="ev-inline">
      {!record ? (
        <button
          className="ev-secondary"
          type="button"
          onClick={async () => {
            try {
              setRecord(await platform(`/artifacts/${id}`));
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          <Download size={14} />
          {id.slice(0, 8)}
        </button>
      ) : record.status === "ready" ? (
        <>
          <a className="ev-secondary" href={`/api/v1/artifacts/${id}/download`}>
            <Download size={14} />
            Download Evidence
          </a>
          {record.content_type === "audio/wav" && (
            <audio
              controls
              preload="none"
              src={`/api/v1/artifacts/${id}/download`}
            />
          )}
        </>
      ) : (
        <span>{record.status}</span>
      )}
      {error && (
        <span role="alert" className="ev-error">
          {error}
        </span>
      )}
    </div>
  );
}

export function EvidenceInspector({ runId, revisionKey }: { runId: string; revisionKey: string }) {
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [events, setEvents] = useState<EventsPage>({
    items: [],
    next_after: 0,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState("observations");
  useEffect(() => {
    let active = true;
    Promise.all([
      platform<EvidencePage>(`/runs/${runId}/results?limit=200`),
      platform<EventsPage>(`/runs/${runId}/events?limit=200`),
    ])
      .then(([evidence, timeline]) => {
        if (active) {
          setPage(evidence);
          setEvents(timeline);
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [runId, revisionKey]);
  async function load(more: boolean) {
    setBusy(true);
    setError("");
    try {
      const [evidence, timeline] = await Promise.all([
        more && page && !page.next_cursor
          ? Promise.resolve(page)
          : platform<EvidencePage>(
              `/runs/${runId}/results?limit=200${more && page?.next_cursor ? `&after=${page.next_cursor}` : ""}`,
            ),
        platform<EventsPage>(
          `/runs/${runId}/events?limit=200&after=${more ? events.next_after : 0}`,
        ),
      ]);
      setPage((old) => ({
        ...evidence,
        items:
          more && old?.next_cursor
            ? [...old.items, ...evidence.items]
            : evidence.items,
      }));
      setEvents((old) => ({
        ...timeline,
        items: more ? [...old.items, ...timeline.items] : timeline.items,
      }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <h3>Evidence Inspector</h3>
      <div className="ev-toolbar">
        <div role="tablist" aria-label="Evidence view">
          {["observations", "checks", "events"].map((value) => (
            <button
              key={value}
              role="tab"
              aria-selected={view === value}
              className={view === value ? "ev-primary" : "ev-secondary"}
              onClick={() => setView(value)}
            >
              {value}
            </button>
          ))}
        </div>
        <button
          className="ev-icon"
          title="Refresh evidence"
          aria-label="Refresh evidence"
          disabled={busy}
          onClick={() => void load(false)}
        >
          <RefreshCw size={16} />
        </button>
      </div>
      {error && (
        <div className="ev-error" role="alert">
          {error}
        </div>
      )}
      {page?.retention && (
        <div className="ev-notice">{page.retention.reason}</div>
      )}
      {view === "events"
        ? events.items.map((event) => (
            <details className="ev-details" key={event.event_id}>
              <summary>
                #{event.sequence} · {event.event_type.replaceAll("_", " ")} ·{" "}
                {event.attempt_id.slice(0, 8)}
              </summary>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre>
            </details>
          ))
        : page?.items
            .filter(
              (item) =>
                item.kind === (view === "checks" ? "check" : "observation"),
            )
            .map((item) => {
              const observation =
                item.kind === "observation" ? (item.data as Observation) : null;
              const check =
                item.kind === "check" ? (item.data as CheckResult) : null;
              return (
                <details className="ev-details" key={item.id}>
                  <summary>
                    {observation?.source ?? check?.check_version_id} ·{" "}
                    {observation?.availability ?? check?.verdict}
                  </summary>
                  {observation && (
                    <div className="ev-inline">
                      <span>{observation.completeness}</span>
                      <span>{observation.storage_mode}</span>
                      <span>
                        {observation.window_seconds}s ·{" "}
                        {observation.window_elapsed
                          ? "window elapsed"
                          : "window incomplete"}
                      </span>
                    </div>
                  )}
                  {check && <p>{check.reason}</p>}
                  <pre>
                    {JSON.stringify(
                      observation?.values ?? check?.measured,
                      null,
                      2,
                    )}
                  </pre>
                  {observation?.artifact_ids?.map((id) => (
                    <EvidenceArtifact key={id} id={id} />
                  ))}
                  <small>
                    Recorded digest: {item.content_hash}
                    <br />
                    Authorized view: {item.view_hash}
                  </small>
                </details>
              );
            })}
      {page && page.items.length === 0 && events.items.length === 0 && (
        <p className="ev-muted">No evidence received.</p>
      )}
      {(page?.next_cursor || events.items.length >= 200) && (
        <button
          className="ev-secondary"
          disabled={busy}
          onClick={() => void load(true)}
        >
          Load More Evidence
        </button>
      )}
    </section>
  );
}
