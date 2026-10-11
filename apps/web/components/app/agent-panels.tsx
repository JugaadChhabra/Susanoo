"use client";

import { RotateCw } from "lucide-react";
import { caughtPercent, problemsLine, type AgentCard as AgentCardData, type ReportCard as ReportCardData } from "@/lib/agent";
import { useAsync } from "@/lib/use-async";
import { getAgentCard, getReportCard } from "@/lib/agent-api";

export function PanelShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-border p-4 sm:p-5">
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  );
}

export function PanelError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert">
      <p className="text-sm text-red-600">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <RotateCw className="size-4" aria-hidden /> Try again
      </button>
    </div>
  );
}

const Skeleton = ({ label }: { label: string }) => <div className="h-28 animate-pulse rounded-xl bg-muted" aria-busy="true" aria-label={label} />;

function List({ heading, items }: { heading: string; items: string[] }) {
  return (
    <div>
      <h3 className="text-sm font-medium">{heading}</h3>
      <ul className="mt-1.5 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function AgentCardView({ card }: { card: AgentCardData }) {
  if (!card.summary && card.can_do.length === 0 && card.cannot_do.length === 0) {
    return <p className="text-sm text-muted-foreground">No description yet. It appears once the agent has been built.</p>;
  }
  return (
    <div className="space-y-4">
      <p className="text-sm">{card.summary}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        {card.can_do.length > 0 && <List heading="What it can do" items={card.can_do} />}
        {card.cannot_do.length > 0 && <List heading="What it can't do" items={card.cannot_do} />}
      </div>
    </div>
  );
}

export function AgentCard({ runId }: { runId: string }) {
  const state = useAsync(() => getAgentCard(runId), runId);
  return (
    <PanelShell title="Your agent">
      {state.status === "loading" && <Skeleton label="Loading your agent" />}
      {state.status === "error" && <PanelError message={state.message} onRetry={state.retry} />}
      {state.status === "ready" && <AgentCardView card={state.data} />}
    </PanelShell>
  );
}

export function ReportCardView({ report }: { report: ReportCardData }) {
  if (report.total <= 0 && report.history.length === 0) {
    return <p className="text-sm text-muted-foreground">No scores yet. They appear after the first round of tests.</p>;
  }
  return (
    <div className="space-y-4">
      <p className="text-sm font-medium">{report.headline || problemsLine(report.caught, report.total, report.false_alarms)}</p>
      {report.history.length > 0 && (
        <ul className="space-y-2">
          {report.history.map((round, i) => (
            <li key={`${round.label}-${i}`} className="text-sm">
              <div className="flex justify-between gap-2 text-muted-foreground">
                <span>{round.label}</span>
                <span>
                  {round.caught} of {round.total} caught, {round.false_alarms} false {round.false_alarms === 1 ? "alarm" : "alarms"}
                </span>
              </div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-muted" role="presentation">
                <div className="h-full rounded-full bg-foreground" style={{ width: `${caughtPercent(round.caught, round.total)}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ReportCard({ runId }: { runId: string }) {
  const state = useAsync(() => getReportCard(runId), runId);
  return (
    <PanelShell title="Report card">
      {state.status === "loading" && <Skeleton label="Loading the report card" />}
      {state.status === "error" && <PanelError message={state.message} onRetry={state.retry} />}
      {state.status === "ready" && <ReportCardView report={state.data} />}
    </PanelShell>
  );
}
