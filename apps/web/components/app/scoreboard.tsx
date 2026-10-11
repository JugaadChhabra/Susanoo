"use client";

import { useId } from "react";
import { TrendingUp } from "lucide-react";
import type { IterationMetrics } from "@/types/generated";
import { cn } from "@/lib/utils";
import type { Budget } from "@/lib/scoreboard";

const SERIES: { key: "recall" | "precision" | "heldout_f1"; label: string; hint: string; stroke: string; swatch: string }[] = [
  { key: "recall", label: "Problems caught", hint: "recall", stroke: "#818cf8", swatch: "bg-indigo-400" },
  { key: "precision", label: "Alarms that were right", hint: "precision", stroke: "#34d399", swatch: "bg-emerald-400" },
  { key: "heldout_f1", label: "Held-out score", hint: "held-out F1", stroke: "#fbbf24", swatch: "bg-amber-400" },
];

const W = 320;
const H = 160;
const PAD = { l: 30, r: 12, t: 10, b: 24 };
const pct = (v: number) => `${Math.round(v * 100)}%`;
const usd = (v: number) => `$${v.toFixed(2)}`;

function Chart({ metrics }: { metrics: IterationMetrics[] }) {
  const maxRound = Math.max(1, ...metrics.map((m) => m.iteration));
  const x = (round: number) => PAD.l + (round / maxRound) * (W - PAD.l - PAD.r);
  const y = (v: number) => PAD.t + (1 - Math.min(1, Math.max(0, v))) * (H - PAD.t - PAD.b);
  const rounds = Array.from({ length: maxRound + 1 }, (_, i) => i);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Line chart of scores for each test round. The values are also listed in the table below.">
      {[0, 0.5, 1].map((t) => (
        <g key={t}>
          <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="#1e293b" strokeWidth={1} />
          <text x={PAD.l - 6} y={y(t) + 3} textAnchor="end" fontSize={9} fill="#64748b">{pct(t)}</text>
        </g>
      ))}
      {rounds.map((r) => (
        <text key={r} x={x(r)} y={H - 8} textAnchor="middle" fontSize={9} fill="#64748b">{r === 0 ? "Start" : `R${r}`}</text>
      ))}
      {SERIES.map((s) => (
        <g key={s.key}>
          {metrics.length > 1 && (
            <polyline
              points={metrics.map((m) => `${x(m.iteration)},${y(m[s.key])}`).join(" ")}
              fill="none"
              stroke={s.stroke}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              className="transition-all duration-500"
            />
          )}
          {metrics.map((m) => (
            <circle key={m.iteration} cx={x(m.iteration)} cy={y(m[s.key])} r={3} fill={s.stroke} className="animate-in fade-in zoom-in-50 duration-500" />
          ))}
        </g>
      ))}
    </svg>
  );
}

function CostBar({ budget }: { budget: Budget }) {
  const labelId = useId();
  const { capUsd, spentUsd } = budget;
  const hasCap = capUsd !== null && capUsd > 0;
  const ratio = hasCap ? Math.min(1, spentUsd / capUsd) : 0;
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span id={labelId} className="text-slate-400">Spend so far</span>
        <span className="tabular-nums text-slate-200">{usd(spentUsd)}{hasCap && <span className="text-slate-500"> of {usd(capUsd)} limit</span>}</span>
      </div>
      {hasCap && (
        <div
          role="progressbar"
          aria-labelledby={labelId}
          aria-valuemin={0}
          aria-valuemax={capUsd}
          aria-valuenow={Math.min(spentUsd, capUsd)}
          aria-valuetext={`${usd(spentUsd)} of ${usd(capUsd)}`}
          className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-800"
        >
          <div className={cn("h-full rounded-full transition-all duration-500", ratio > 0.85 ? "bg-amber-400" : "bg-indigo-400")} style={{ width: `${ratio * 100}%` }} />
        </div>
      )}
    </div>
  );
}

export function Scoreboard({ metrics, budget }: { metrics: IterationMetrics[]; budget: Budget }) {
  const latest = metrics.at(-1);
  return (
    <div className="space-y-4 p-4">
      {!latest ? (
        <div className="flex items-center gap-3 rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-400">
          <TrendingUp className="size-4 shrink-0" aria-hidden />
          <p>No scores yet. The first one appears once Susanoo has run its first round of tests.</p>
        </div>
      ) : (
        <>
          <ul className="grid grid-cols-3 gap-2">
            {SERIES.map((s) => (
              <li key={s.key} className="rounded-lg bg-slate-900 p-2.5 ring-1 ring-slate-800">
                <p className="flex items-center gap-1.5 text-[11px] leading-tight text-slate-400">
                  <span className={cn("size-2 shrink-0 rounded-full", s.swatch)} aria-hidden /> {s.label}
                </p>
                <p className="mt-1 text-lg font-semibold tabular-nums">{pct(latest[s.key])}</p>
              </li>
            ))}
          </ul>
          <Chart metrics={metrics} />
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer rounded focus-visible:outline-2 focus-visible:outline-indigo-400">Show the numbers</summary>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full min-w-[20rem] text-left tabular-nums">
                <caption className="sr-only">Scores for each test round</caption>
                <thead>
                  <tr className="text-slate-500">
                    <th scope="col" className="py-1 pr-2 font-medium">Round</th>
                    {SERIES.map((s) => <th key={s.key} scope="col" className="py-1 pr-2 font-medium">{s.label}</th>)}
                    <th scope="col" className="py-1 font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.map((m) => (
                    <tr key={m.iteration} className="border-t border-slate-800">
                      <th scope="row" className="py-1 pr-2 font-normal">{m.iteration === 0 ? "Start" : m.iteration}</th>
                      {SERIES.map((s) => <td key={s.key} className="py-1 pr-2">{pct(m[s.key])}</td>)}
                      <td className="py-1">{usd(m.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
      <CostBar budget={budget} />
    </div>
  );
}
