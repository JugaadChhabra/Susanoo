"use client";

import { useState, type FormEvent } from "react";
import { CheckCircle2, Loader2, RotateCw, XCircle } from "lucide-react";
import { listConnectors, revokeConnection, saveConnection, testConnection } from "@/lib/api";
import { connectionLabel, connectionStatusLabel, guideSegments, missingField, writesLabel } from "@/lib/connectors";
import { useAsync } from "@/lib/use-async";
import { cn } from "@/lib/utils";
import type { ConnectionSummary, ConnectionTest, ConnectorInfo } from "@/types/generated";

const outlineButton =
  "inline-flex min-h-10 items-center justify-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";
const primaryButton =
  "inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";
const field = "mt-1 w-full rounded-lg border border-border bg-background p-3 text-sm";

const STATUS_TONE: Record<ConnectionSummary["status"], string> = {
  connected: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  untested: "bg-muted text-muted-foreground",
  failed: "bg-red-500/15 text-red-700 dark:text-red-300",
};

const errorText = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

/** Available connectors, each with its setup guide beside the fields. [PRD › Frontend and API › Screens › Connectors] */
export function ConnectorList() {
  const state = useAsync(listConnectors, "connectors");

  if (state.status === "loading") {
    return (
      <div className="space-y-3" aria-busy="true" aria-label="Loading connectors">
        {[0, 1].map((i) => (
          <div key={i} className="h-48 animate-pulse rounded-xl bg-muted" />
        ))}
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div role="alert" className="rounded-xl border border-red-500/40 p-4">
        <p className="text-sm text-red-600">{state.message}</p>
        <button type="button" onClick={state.retry} className={cn(outlineButton, "mt-3")}>
          <RotateCw className="size-4" aria-hidden /> Try again
        </button>
      </div>
    );
  }

  if (state.data.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border p-6 text-center sm:p-10">
        <p className="font-medium">No connectors available right now</p>
        <p className="mt-1 text-sm text-muted-foreground">Your agents can still run on example data. Check back soon.</p>
      </div>
    );
  }

  return (
    <ul className="space-y-6">
      {state.data.map((info) => (
        <ConnectorForm key={info.spec.id} info={info} />
      ))}
    </ul>
  );
}

type Busy = "saving" | "testing" | "revoking" | null;

/** One connector: its guide, then either the saved connection (test, revoke, replace) or the credential form. */
export function ConnectorForm({ info }: { info: ConnectorInfo }) {
  const { spec, guide } = info;
  const [connection, setConnection] = useState<ConnectionSummary | null>(info.connection);
  const [replacing, setReplacing] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionTest | null>(null);
  const headingId = `connector-${spec.id}`;

  async function runTest(id: string) {
    setBusy("testing");
    setError(null);
    try {
      const tested = await testConnection(id);
      setResult(tested);
      setConnection(tested.connection);
    } catch (e) {
      setError(errorText(e, "We couldn't test this connection."));
    } finally {
      setBusy(null);
    }
  }

  async function save(values: Record<string, string>, writesEnabled: boolean) {
    setBusy("saving");
    setError(null);
    setResult(null);
    let saved: ConnectionSummary;
    try {
      saved = await saveConnection({ connector: spec.id, values, writes_enabled: writesEnabled });
    } catch (e) {
      setError(errorText(e, "We couldn't save this connection."));
      setBusy(null);
      return;
    }
    setConnection(saved);
    setReplacing(false);
    await runTest(saved.id); // check the credential right after entry
  }

  async function revoke() {
    if (!connection) return;
    if (!window.confirm(`Revoke ${spec.name}? Your agents will lose access straight away.`)) return;
    setBusy("revoking");
    setError(null);
    try {
      await revokeConnection(connection.id);
      setConnection(null);
      setResult(null);
    } catch (e) {
      setError(errorText(e, "We couldn't revoke this connection."));
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="min-w-0 rounded-xl border border-border p-4 sm:p-6" aria-labelledby={headingId}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id={headingId} className="text-lg font-semibold">
          {spec.name}
        </h2>
        {connection && (
          <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", STATUS_TONE[connection.status])}>
            {connectionStatusLabel(connection.status)}
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-muted-foreground">{spec.summary}</p>

      <div className="mt-4 grid gap-6 md:grid-cols-2">
        <Guide steps={guide.steps} notes={guide.notes} />

        <div className="min-w-0">
          {connection && !replacing ? (
            <div className="space-y-3">
              <p className="text-sm font-medium break-words">{connectionLabel(spec, connection)}</p>
              {spec.actions.some((a) => a.write) && <p className="text-sm text-muted-foreground">{writesLabel(connection)}</p>}
              <div className="flex flex-wrap gap-2">
                <button type="button" onClick={() => runTest(connection.id)} disabled={busy !== null} className={outlineButton}>
                  {busy === "testing" && <Loader2 className="size-4 animate-spin" aria-hidden />} Test connection
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setReplacing(true);
                    setResult(null);
                    setError(null);
                  }}
                  disabled={busy !== null}
                  className={outlineButton}
                >
                  Replace token
                </button>
                <button
                  type="button"
                  onClick={revoke}
                  disabled={busy !== null}
                  className={cn(outlineButton, "border-red-500/40 text-red-700 hover:bg-red-500/10 dark:text-red-300")}
                >
                  {busy === "revoking" && <Loader2 className="size-4 animate-spin" aria-hidden />} Revoke
                </button>
              </div>
            </div>
          ) : (
            <CredentialFields
              info={info}
              initialSettings={connection?.settings ?? {}}
              initialWrites={connection?.writes_enabled ?? false}
              busy={busy}
              onSave={save}
              onCancel={connection ? () => setReplacing(false) : undefined}
            />
          )}

          <div aria-live="polite" className="mt-3 space-y-2">
            {busy === "testing" && <p className="text-sm text-muted-foreground">Checking with {spec.name}…</p>}
            {result && busy === null && (
              <p
                className={cn(
                  "flex items-start gap-2 rounded-lg p-3 text-sm break-words",
                  result.ok ? "bg-emerald-500/10 text-emerald-800 dark:text-emerald-200" : "bg-red-500/10 text-red-800 dark:text-red-200",
                )}
              >
                {result.ok ? (
                  <CheckCircle2 className="mt-0.5 size-4 shrink-0" aria-hidden />
                ) : (
                  <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
                )}
                {result.message}
              </p>
            )}
            {error && (
              <p role="alert" className="text-sm text-red-600">
                {error}
              </p>
            )}
          </div>
        </div>
      </div>
    </li>
  );
}

function Guide({ steps, notes }: { steps: string[]; notes: string[] }) {
  return (
    <div className="min-w-0 rounded-lg bg-muted/50 p-4 text-sm">
      <p className="font-medium">How to set it up</p>
      <ol className="mt-2 list-decimal space-y-2 pl-5">
        {steps.map((step, i) => (
          <li key={i} className="break-words">
            <GuideText text={step} />
          </li>
        ))}
      </ol>
      {notes.map((note, i) => (
        <p key={i} className="mt-3 break-words text-muted-foreground">
          <GuideText text={note} />
        </p>
      ))}
    </div>
  );
}

function GuideText({ text }: { text: string }) {
  return (
    <>
      {guideSegments(text).map((seg, i) =>
        seg.kind === "bold" ? (
          <strong key={i} className="font-semibold">
            {seg.text}
          </strong>
        ) : seg.kind === "link" ? (
          <a key={i} href={seg.href} target="_blank" rel="noreferrer" className="underline underline-offset-2">
            {seg.text}
          </a>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}

function CredentialFields({
  info,
  initialSettings,
  initialWrites,
  busy,
  onSave,
  onCancel,
}: {
  info: ConnectorInfo;
  initialSettings: Record<string, string>;
  initialWrites: boolean;
  busy: Busy;
  onSave: (values: Record<string, string>, writesEnabled: boolean) => Promise<void>;
  onCancel?: () => void;
}) {
  const { spec } = info;
  const [values, setValues] = useState<Record<string, string>>(() => ({ ...initialSettings }));
  const [writes, setWrites] = useState(initialWrites);
  const [problem, setProblem] = useState<string | null>(null);
  const writeActions = spec.actions.filter((a) => a.write);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const missing = missingField(spec, values);
    if (missing) {
      setProblem(missing);
      return;
    }
    setProblem(null);
    const trimmed = Object.fromEntries(Object.entries(values).map(([k, v]) => [k, v.trim()]));
    // Drop the secret from memory once it's sent, whether or not the save worked.
    const secretNames = spec.fields.filter((f) => f.secret).map((f) => f.name);
    setValues((prev) => Object.fromEntries(Object.entries(prev).filter(([k]) => !secretNames.includes(k))));
    await onSave(trimmed, writes);
  }

  return (
    <form onSubmit={submit} className="space-y-4" autoComplete="off">
      {spec.fields.map((f) => {
        const id = `${spec.id}-${f.name}`;
        return (
          <div key={f.name}>
            <label htmlFor={id} className="text-sm font-medium">
              {f.label}
            </label>
            <p id={`${id}-help`} className="text-sm text-muted-foreground">
              {f.help}
            </p>
            <input
              id={id}
              type={f.secret ? "password" : "text"}
              value={values[f.name] ?? ""}
              onChange={(e) => setValues((prev) => ({ ...prev, [f.name]: e.target.value }))}
              placeholder={f.placeholder}
              aria-describedby={`${id}-help`}
              autoComplete="off"
              spellCheck={false}
              required
              maxLength={1000}
              className={field}
            />
          </div>
        );
      })}

      {writeActions.length > 0 && (
        <label className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm">
          <input type="checkbox" className="mt-0.5 size-4" checked={writes} onChange={(e) => setWrites(e.target.checked)} />
          <span>
            <span className="font-medium">Let the agent act, not just read</span>
            <span className="block text-muted-foreground">
              {writeActions.map((a) => a.description).join(". ")}. Off means a dry run: Susanoo only tells you what it
              would have done.
            </span>
          </span>
        </label>
      )}

      {problem && (
        <p role="alert" className="text-sm text-red-600">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy !== null} className={cn(primaryButton, "w-full sm:w-auto")}>
          {busy === "saving" && <Loader2 className="size-4 animate-spin" aria-hidden />} Save and test
        </button>
        {onCancel && (
          <button type="button" onClick={onCancel} disabled={busy !== null} className={outlineButton}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
