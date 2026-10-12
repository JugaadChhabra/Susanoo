"use client";

import { useCallback, useMemo, useState } from "react";
import Link from "next/link";
import { Bot, Hand, RotateCw, WifiOff } from "lucide-react";
import { decideApproval, isStepMovedOn, STEP_MOVED_ON } from "@/lib/api";
import { deriveBudget, deriveLedger, deriveMetrics, derivePendingApproval, gateApproval } from "@/lib/scoreboard";
import { cn } from "@/lib/utils";
import { deriveRunState, type RunStatus } from "@/lib/run-state";
import { useRunEvents, type RunSource } from "@/lib/use-run-events";
import { useRunPause } from "@/lib/use-run-pause";
import { EventLog } from "./event-log";
import { HeadsGraph } from "./heads-graph";
import { Scoreboard } from "./scoreboard";
import { FailureLedger } from "./failure-ledger";
import { ApprovalCard } from "./approval-card";

const STATUS_PILL: Record<RunStatus, { text: string; cls: string }> = {
  waiting: { text: "Getting ready", cls: "bg-slate-800 text-slate-300" },
  running: { text: "Running", cls: "bg-indigo-500/20 text-indigo-200" },
  paused: { text: "Waiting for you", cls: "bg-amber-500/20 text-amber-200" },
  complete: { text: "Finished", cls: "bg-emerald-500/20 text-emerald-200" },
  failed: { text: "Stopped with an error", cls: "bg-red-500/20 text-red-200" },
};

/** One run view for live, fixture and demo replay: they differ only in `source`. */
export function RunView({ source }: { source: RunSource }) {
  const { events, status, error, restart } = useRunEvents(source);
  const snapshot = useMemo(() => deriveRunState(events), [events]);
  const metrics = useMemo(() => deriveMetrics(events), [events]);
  const budget = useMemo(() => deriveBudget(events), [events]);
  const ledger = useMemo(() => deriveLedger(events), [events]);
  const replayApproval = useMemo(() => derivePendingApproval(events), [events]);
  const liveRunId = source.kind === "live" ? source.runId : null;
  const lastPause = useMemo(() => events.findLast((e) => e.type === "question_needed" || e.type === "approval_needed")?.seq ?? 0, [events]);
  const { intake: pause, failing: pauseFailing, reload: reloadPause } = useRunPause(liveRunId, String(lastPause), snapshot.status === "paused");
  // A notice belongs to the pause it was about; a new pause clears it.
  const [notice, setNotice] = useState<{ pause: number; text: string } | null>(null);
  const noticeText = notice?.pause === lastPause ? notice.text : null;
  // The pause the user just answered: until the run moves on, say so instead of asking again.
  const [decidedToken, setDecidedToken] = useState<string | null>(null);
  const pauseToken = pause?.pause_token ?? null;
  const decided = pauseToken !== null && pauseToken === decidedToken;
  const gate = pause?.kind === "gate" && !decided ? pause.gate : null;
  const approval = liveRunId ? (gate ? gateApproval(gate, events) : null) : replayApproval;
  const onDecide = useCallback(
    async (approvalId: string, decision: "approve" | "reject") => {
      if (!liveRunId || !pauseToken) return;
      setNotice(null);
      try {
        await decideApproval(liveRunId, approvalId, { pause_token: pauseToken, approved: decision === "approve" });
        setDecidedToken(pauseToken);
      } catch (e) {
        if (!isStepMovedOn(e)) throw e;
        setNotice({ pause: lastPause, text: STEP_MOVED_ON });
        reloadPause();
        throw new Error(STEP_MOVED_ON); // the card says it; once it's gone, the notice below does
      }
    },
    [liveRunId, pauseToken, lastPause, reloadPause],
  );
  const needsAnswers = liveRunId !== null && !decided && (pause?.kind === "questions" || pause?.kind === "scenarios");
  const pill = STATUS_PILL[snapshot.status];
  const reconnecting = status === "reconnecting";

  return (
    <div className="space-y-4 rounded-2xl bg-slate-950 p-3 text-slate-100 shadow-xl ring-1 ring-slate-800 sm:p-5">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 px-1">
        <span className={cn("rounded-full px-3 py-1 text-xs font-medium", pill.cls)}>
          {snapshot.status === "running" && (
            <span className="mr-1.5 inline-block size-1.5 animate-pulse rounded-full bg-indigo-300 align-middle" />
          )}
          {pill.text}
        </span>
        {snapshot.iteration > 0 && (
          <span className="flex items-center gap-1.5 text-xs text-slate-400">
            <RotateCw className="size-3" aria-hidden /> Test round {snapshot.iteration}
          </span>
        )}
        {liveRunId && (
          <Link
            href={`/runs/${encodeURIComponent(liveRunId)}/agent`}
            className="ml-auto inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2 text-xs text-slate-300 hover:bg-slate-800 hover:text-white focus-visible:outline-2 focus-visible:outline-slate-300"
          >
            <Bot className="size-3.5" aria-hidden /> Your agent
          </Link>
        )}
        {source.kind !== "live" && (
          <span className="rounded-full border border-slate-700 px-2.5 py-0.5 text-[11px] text-slate-400">
            Replay of a saved run
          </span>
        )}
        {reconnecting && (
          <span role="status" className="flex items-center gap-1.5 text-xs text-amber-300">
            <WifiOff className="size-3" aria-hidden /> Connection dropped, reconnecting…
          </span>
        )}
      </header>

      <section aria-label="Run progress" className="overflow-hidden rounded-xl bg-slate-900/40 ring-1 ring-slate-800">
        <HeadsGraph snapshot={snapshot} />
      </section>

      {approval && (
        <ApprovalCard key={pauseToken ?? approval.patchId} approval={approval} onDecide={liveRunId ? onDecide : undefined} />
      )}

      {noticeText && !approval && (
        <p role="status" className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/30">
          {noticeText}
        </p>
      )}

      {decided && snapshot.status === "paused" && (
        <p role="status" className="rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/30">
          Answer sent. Waiting for Susanoo to carry on…
        </p>
      )}

      {needsAnswers && liveRunId && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/30">
          <Hand className="size-4 shrink-0" aria-hidden />
          <p className="min-w-0 flex-1">
            {pause?.kind === "questions"
              ? "Susanoo has a few quick questions for you before it builds."
              : "Susanoo wrote some example scenarios. Approve, edit or reject them so it can build."}
          </p>
          <Link
            href={`/runs/${encodeURIComponent(liveRunId)}/intake`}
            className="inline-flex min-h-10 items-center rounded-lg bg-amber-400 px-4 text-sm font-medium text-amber-950 hover:bg-amber-300 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-300"
          >
            {pause?.kind === "questions" ? "Answer the questions" : "Review the examples"}
          </Link>
        </div>
      )}

      {snapshot.status === "paused" && !approval && !needsAnswers && !decided && (
        <div role="status" className="flex items-start gap-3 rounded-xl bg-amber-500/10 p-4 text-sm text-amber-100 ring-1 ring-amber-500/30">
          <Hand className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p>
            Susanoo is waiting on a decision from you before it carries on.
            {pauseFailing && " We're having trouble loading it right now and will keep trying."}
          </p>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <section aria-label="Scoreboard" className="overflow-hidden rounded-xl bg-slate-900/40 ring-1 ring-slate-800">
          <h2 className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">Scoreboard</h2>
          <Scoreboard metrics={metrics} budget={budget} />
        </section>
        <section aria-label="What went wrong and what we did" className="overflow-hidden rounded-xl bg-slate-900/40 ring-1 ring-slate-800">
          <h2 className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">What went wrong, and what we did</h2>
          <FailureLedger rows={ledger} finished={snapshot.status === "complete"} />
        </section>
      </div>

      <section aria-label="What's happening" className="overflow-hidden rounded-xl bg-slate-900/40 ring-1 ring-slate-800">
        <h2 className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">What&apos;s happening</h2>
        <EventLog events={events} loading={status === "connecting" || status === "reconnecting"} error={error} onRetry={restart} />
      </section>
    </div>
  );
}
