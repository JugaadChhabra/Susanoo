"use client";

import { ArrowRight, Ban, Check, Clock, Hand, Loader2, RotateCcw, SearchX } from "lucide-react";
import { cn } from "@/lib/utils";
import { LAYER_LABELS } from "@/lib/run-state";
import type { LedgerOutcome, LedgerRow } from "@/lib/scoreboard";

const LAYER_STYLE: Record<string, string> = {
  code: "bg-sky-500/15 text-sky-200 ring-sky-500/30",
  tool: "bg-violet-500/15 text-violet-200 ring-violet-500/30",
  prompt_policy: "bg-indigo-500/15 text-indigo-200 ring-indigo-500/30",
  test: "bg-amber-500/15 text-amber-200 ring-amber-500/30",
};

const OUTCOME: Record<LedgerOutcome, { text: string; cls: string; icon: typeof Check }> = {
  pending: { text: "Not tried yet", cls: "bg-slate-800 text-slate-300", icon: Clock },
  testing: { text: "Checking the fix", cls: "bg-indigo-500/20 text-indigo-200", icon: Loader2 },
  awaiting_approval: { text: "Needs your OK", cls: "bg-amber-500/20 text-amber-200", icon: Hand },
  kept: { text: "Kept", cls: "bg-emerald-500/20 text-emerald-200", icon: Check },
  rolled_back: { text: "Rolled back", cls: "bg-red-500/20 text-red-200", icon: RotateCcw },
  rejected: { text: "You said no", cls: "bg-slate-700 text-slate-200", icon: Ban },
};

const pct = (v: number) => `${Math.round(v * 100)}%`;

function Row({ row }: { row: LedgerRow }) {
  const o = OUTCOME[row.outcome];
  const Icon = o.icon;
  return (
    <li className="space-y-2 px-4 py-3 animate-in fade-in slide-in-from-bottom-1 duration-300">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn("rounded-full px-2.5 py-0.5 text-xs font-medium ring-1", LAYER_STYLE[row.layer])}>{LAYER_LABELS[row.layer]}</span>
        <span className={cn("ml-auto inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium", o.cls)}>
          <Icon className={cn("size-3", row.outcome === "testing" && "animate-spin motion-reduce:animate-none")} aria-hidden /> {o.text}
        </span>
      </div>
      <p className="break-words text-sm text-slate-200">{row.hypothesis}</p>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        {row.scenarioCount > 0 && <span>{row.scenarioCount} failing {row.scenarioCount === 1 ? "situation" : "situations"}</span>}
        {row.linesChanged !== null && <span>{row.linesChanged} lines changed</span>}
        {row.heldoutBefore !== null && row.heldoutAfter !== null && (
          <span className="inline-flex items-center gap-1 text-slate-400">
            Held-out score {pct(row.heldoutBefore)} <ArrowRight className="size-3" aria-hidden /> {pct(row.heldoutAfter)}
          </span>
        )}
      </p>
    </li>
  );
}

export function FailureLedger({ rows, finished }: { rows: LedgerRow[]; finished: boolean }) {
  if (rows.length === 0) {
    return (
      <div className="m-4 flex items-center gap-3 rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-400">
        <SearchX className="size-4 shrink-0" aria-hidden />
        <p>{finished ? "Nothing needed fixing." : "Nothing has failed yet. When a test fails, the cause and the fix show up here."}</p>
      </div>
    );
  }
  return <ul className="divide-y divide-slate-800">{rows.map((r) => <Row key={r.id} row={r} />)}</ul>;
}
