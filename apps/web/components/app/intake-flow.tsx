"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, RotateCw } from "lucide-react";
import { canBuild, countPending, intakeStep, toDecisionPayload, type Intake, type ScenarioDecision } from "@/lib/intake";
import { getIntake, submitAnswers, submitScenarios } from "@/lib/intake-api";
import { IntakeForm } from "./intake-form";
import { ScenarioCard } from "./scenario-card";

const POLL_MS = 2500;

/** Questions first, then scenario cards; Build unlocks only once every card is decided and one is approved. */
export function IntakeFlow({ runId }: { runId: string }) {
  const router = useRouter();
  const [intake, setIntake] = useState<Intake | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answersSent, setAnswersSent] = useState(false);
  const [decisions, setDecisions] = useState<Record<string, ScenarioDecision>>({});
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const step = intake ? intakeStep(intake, answersSent) : null;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let hasLoaded = false;
    const load = async () => {
      try {
        const next = await getIntake(runId);
        if (cancelled) return;
        hasLoaded = true;
        setIntake(next);
        setError(null);
        // Keep checking while Susanoo is still writing the questions or scenarios.
        if (intakeStep(next, answersSent) === "waiting") timer = setTimeout(load, POLL_MS);
      } catch (e) {
        if (cancelled) return;
        if (hasLoaded) timer = setTimeout(load, POLL_MS); // a blip while polling retries quietly
        else setError(e instanceof Error ? e.message : "We couldn't load your questions.");
      }
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [runId, answersSent, attempt]);

  const sendAnswers = useCallback(
    async (answers: Record<string, string>) => {
      await submitAnswers(runId, answers);
      setAnswersSent(true);
    },
    [runId],
  );

  async function build() {
    if (!intake) return;
    setBuilding(true);
    setBuildError(null);
    try {
      await submitScenarios(runId, toDecisionPayload(intake.scenarios, decisions));
      router.push(`/runs/${encodeURIComponent(runId)}`);
    } catch (e) {
      setBuildError(e instanceof Error ? e.message : "We couldn't send your choices.");
      setBuilding(false);
    }
  }

  if (error) {
    return (
      <div role="alert" className="rounded-xl border border-red-500/40 p-4">
        <p className="text-sm text-red-600">{error}</p>
        <button
          type="button"
          onClick={() => {
            setError(null);
            setAttempt((n) => n + 1);
          }}
          className="mt-3 inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted"
        >
          <RotateCw className="size-4" aria-hidden /> Try again
        </button>
      </div>
    );
  }

  if (!intake) {
    return <div className="h-64 animate-pulse rounded-2xl bg-muted" aria-busy="true" aria-label="Loading your questions" />;
  }

  if (step === "questions") {
    return <IntakeForm questions={intake.questions} onSubmit={sendAnswers} />;
  }

  if (step === "waiting") {
    return (
      <div role="status" className="flex items-center gap-3 rounded-xl border border-border p-4 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {answersSent ? "Thanks. Susanoo is writing example scenarios from your answers…" : "Susanoo is reading your spec. Questions or examples will show up here shortly…"}
      </div>
    );
  }

  const pending = countPending(intake.scenarios, decisions);
  const unlocked = canBuild(intake.scenarios, decisions);
  const allRejected = pending === 0 && !unlocked;

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        These are examples of what your agent should do. Approve, edit or reject each one. Approved examples become the tests Susanoo is held to.
      </p>
      <ul className="space-y-3">
        {intake.scenarios.map((card) => (
          <ScenarioCard
            key={card.id}
            card={card}
            decision={decisions[card.id] ?? { action: "pending" }}
            onChange={(d) => setDecisions((cur) => ({ ...cur, [card.id]: d }))}
          />
        ))}
      </ul>
      <div className="space-y-2">
        <button
          type="button"
          disabled={!unlocked || building}
          onClick={build}
          className="inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-50 sm:w-auto"
        >
          {building && <Loader2 className="size-4 animate-spin" aria-hidden />}
          {building ? "Starting…" : "Build my agent"}
        </button>
        {pending > 0 && <p className="text-xs text-muted-foreground">{pending === 1 ? "1 example still needs your decision." : `${pending} examples still need your decision.`}</p>}
        {allRejected && <p role="status" className="text-xs text-muted-foreground">Approve or edit at least one example to continue.</p>}
        {buildError && <p role="alert" className="text-sm text-red-600">{buildError}</p>}
      </div>
    </div>
  );
}
