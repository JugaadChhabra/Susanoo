"use client";

import { useState } from "react";
import { Check, Hand, Loader2, X } from "lucide-react";
import { LAYER_LABELS } from "@/lib/run-state";
import type { PendingApproval } from "@/lib/scoreboard";

/** Test-layer changes pause the run until a human decides ([PRD › Improvement loop and guardrails]). */
export function ApprovalCard({
  approval,
  onDecide,
}: {
  approval: PendingApproval;
  /** Absent in replay modes, where nobody can answer. */
  onDecide?: (patchId: string, decision: "approve" | "reject") => Promise<void>;
}) {
  const [sending, setSending] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(decision: "approve" | "reject") {
    if (!onDecide) return;
    setSending(decision);
    setError(null);
    try {
      await onDecide(approval.patchId, decision);
    } catch (e) {
      setError(e instanceof Error ? e.message : "We couldn't send your answer.");
      setSending(null);
    }
  }

  return (
    <section aria-labelledby="approval-title" className="rounded-xl bg-amber-500/10 p-4 ring-1 ring-amber-500/40 animate-in fade-in slide-in-from-bottom-2 duration-300">
      <h2 id="approval-title" className="flex items-center gap-2 text-sm font-semibold text-amber-100">
        <Hand className="size-4" aria-hidden /> Your OK is needed
      </h2>
      <p className="mt-2 text-sm text-amber-50">
        Susanoo wants to change <strong>{LAYER_LABELS[approval.layer] ?? "the test itself"}</strong>. Changing a test can make results look better without the agent
        improving, so it never happens without you.
      </p>
      <dl className="mt-3 space-y-2 text-sm">
        <div>
          <dt className="text-xs text-amber-200/70">What it wants to do</dt>
          <dd className="text-slate-100">{approval.summary}</dd>
        </div>
        {approval.hypothesis && (
          <div>
            <dt className="text-xs text-amber-200/70">Why</dt>
            <dd className="text-slate-200">{approval.hypothesis}</dd>
          </div>
        )}
      </dl>
      <details className="mt-3 text-xs text-amber-100/80">
        <summary className="cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-amber-300">Details</summary>
        <p className="mt-1 break-all">
          {approval.file && <>File: {approval.file}. </>}
          {approval.linesChanged !== null && <>{approval.linesChanged} lines changed.</>}
        </p>
      </details>
      {onDecide ? (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={sending !== null}
            onClick={() => decide("approve")}
            className="inline-flex min-h-10 items-center gap-1.5 rounded-lg bg-emerald-500 px-4 text-sm font-medium text-emerald-950 hover:bg-emerald-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-300 disabled:opacity-60"
          >
            {sending === "approve" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Check className="size-4" aria-hidden />} Approve
          </button>
          <button
            type="button"
            disabled={sending !== null}
            onClick={() => decide("reject")}
            className="inline-flex min-h-10 items-center gap-1.5 rounded-lg bg-slate-800 px-4 text-sm font-medium text-slate-100 hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-300 disabled:opacity-60"
          >
            {sending === "reject" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <X className="size-4" aria-hidden />} Reject
          </button>
        </div>
      ) : (
        <p className="mt-4 text-xs text-amber-200/80">This is a replay, so the answer is filled in for you in a moment.</p>
      )}
      {error && <p role="alert" className="mt-3 text-sm text-red-300">{error}</p>}
    </section>
  );
}
