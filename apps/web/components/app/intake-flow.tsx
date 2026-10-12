"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Hand, Loader2, RotateCw } from "lucide-react";
import { isStepMovedOn, STEP_MOVED_ON } from "@/lib/api";
import { canBuild, countPending, intakeStep, toScenariosIn, type ScenarioDecision } from "@/lib/intake";
import { getIntake, submitAnswers, submitScenarios, type IntakeState } from "@/lib/intake-api";
import { IntakeForm } from "./intake-form";
import { ScenarioCard } from "./scenario-card";

const POLL_MS = 2500;

const linkClass =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/**
 * Questions first, then scenario cards, on the run's real pauses. Each answer carries the pause_token of
 * the step it answers; a 409 means the step moved on, so we say so and reload.
 */
export function IntakeFlow({ runId }: { runId: string }) {
  const router = useRouter();
  const [state, setState] = useState<IntakeState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [answered, setAnswered] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, ScenarioDecision>>({});
  const [building, setBuilding] = useState(false);
  const [buildError, setBuildError] = useState<string | null>(null);

  const step = state ? intakeStep(state.intake, state.runFinished) : null;
  const runHref = `/runs/${encodeURIComponent(runId)}`;

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let hasLoaded = false;
    const load = async () => {
      try {
        const next = await getIntake(runId);
        if (cancelled) return;
        hasLoaded = true;
        setState(next);
        setError(null);
        // Keep checking while Susanoo is still working out what to ask.
        if (intakeStep(next.intake, next.runFinished) === "waiting") timer = setTimeout(load, POLL_MS);
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
  }, [runId, reload]);

  const movedOn = useCallback(() => {
    setNotice(STEP_MOVED_ON);
    setDecisions({});
    setReload((n) => n + 1);
  }, []);

  const token = state?.intake.pause_token ?? null;

  const sendAnswers = useCallback(
    async (answers: Record<string, string>) => {
      if (!token) return;
      setNotice(null);
      try {
        await submitAnswers(runId, { pause_token: token, answers });
      } catch (e) {
        if (!isStepMovedOn(e)) throw e;
        movedOn();
        return;
      }
      setAnswered(true);
      setReload((n) => n + 1);
    },
    [runId, token, movedOn],
  );

  async function build() {
    if (!state || !token) return;
    setBuilding(true);
    setBuildError(null);
    setNotice(null);
    try {
      await submitScenarios(runId, toScenariosIn(token, state.intake.scenarios, decisions));
      router.push(runHref);
    } catch (e) {
      setBuilding(false);
      if (isStepMovedOn(e)) movedOn();
      else setBuildError(e instanceof Error ? e.message : "We couldn't send your choices.");
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
            setReload((n) => n + 1);
          }}
          className="mt-3 inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted"
        >
          <RotateCw className="size-4" aria-hidden /> Try again
        </button>
      </div>
    );
  }

  if (!state) {
    return <div className="h-64 animate-pulse rounded-2xl bg-muted" aria-busy="true" aria-label="Loading your questions" />;
  }

  const noticeBox = notice && (
    <p role="status" className="mb-4 rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      {notice}
    </p>
  );

  if (step === "questions") {
    return (
      <>
        {noticeBox}
        <IntakeForm key={token} questions={state.intake.questions} onSubmit={sendAnswers} />
      </>
    );
  }

  if (step === "waiting") {
    return (
      <>
        {noticeBox}
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-xl border border-border p-4 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          <span className="min-w-0 flex-1">
            {answered
              ? "Thanks. Susanoo is writing example scenarios from your answers…"
              : "Susanoo is working on your run. Any questions or examples for you will show up here…"}
          </span>
          <Link href={runHref} className={linkClass}>
            Watch the run
          </Link>
        </div>
      </>
    );
  }

  if (step === "gate" || step === "finished") {
    return (
      <>
        {noticeBox}
        <div role="status" className="space-y-3 rounded-xl border border-border p-4 text-sm">
          <p className="flex items-start gap-2">
            {step === "gate" && <Hand className="mt-0.5 size-4 shrink-0" aria-hidden />}
            {step === "gate" ? "Your questions are done. Susanoo now needs your OK on a change to one of its tests." : state.intake.message}
          </p>
          <Link href={runHref} className={linkClass}>
            {step === "gate" ? "Go to your run to decide" : "See your run"}
          </Link>
        </div>
      </>
    );
  }

  const cards = state.intake.scenarios;
  const pending = countPending(cards, decisions);
  const unlocked = canBuild(cards, decisions);
  const allRejected = pending === 0 && !unlocked;

  return (
    <div className="space-y-4">
      {noticeBox}
      <p className="text-sm text-muted-foreground">
        {cards.length > 0
          ? "These are examples of what your agent should do. Approve, edit or reject each one. Approved examples become the tests Susanoo is held to."
          : "Susanoo didn't come up with any examples for you to check this time. You can carry on and it will build from your spec."}
      </p>
      <ul className="space-y-3">
        {cards.map((card) => (
          <ScenarioCard
            key={`${token}:${card.id}`}
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
