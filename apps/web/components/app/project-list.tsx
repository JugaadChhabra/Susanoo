"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, Play, Plus, RotateCw } from "lucide-react";
import { listProjects, startRun, updateProjectRules } from "@/lib/api";
import { formatCost, formatScore, parseRules, rulesProblem, rulesToText, runStatusLabel } from "@/lib/projects";
import { useAsync } from "@/lib/use-async";
import { cn } from "@/lib/utils";
import type { ProjectSummary, RunSummary } from "@/types/generated";

const primaryLink =
  "inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";
const outlineButton =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";

const STATUS_TONE: Record<RunSummary["status"], string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  awaiting_approval: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  done: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  failed: "bg-red-500/15 text-red-700 dark:text-red-300",
  cancelled: "bg-muted text-muted-foreground",
};

/** The signed-in user's projects and their runs. [PRD › Frontend and API › Screens › Projects] */
export function ProjectList() {
  const state = useAsync(listProjects, "projects");
  // Only the rules are overridden after a save, so runs and statuses always come from the latest load.
  const [savedRules, setSavedRules] = useState<Record<string, string[]>>({});

  if (state.status === "loading") {
    return (
      <div className="space-y-3" aria-busy="true" aria-label="Loading your projects">
        {[0, 1].map((i) => (
          <div key={i} className="h-32 animate-pulse rounded-xl bg-muted" />
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
        <p className="font-medium">No projects yet</p>
        <p className="mt-1 text-sm text-muted-foreground">
          Describe the agent you want in plain English and Susanoo will build it, test it and fix it.
        </p>
        <Link href="/projects/new" className={cn(primaryLink, "mt-4")}>
          <Plus className="size-4" aria-hidden /> New project
        </Link>
      </div>
    );
  }

  return (
    <ul className="space-y-4">
      {state.data.map((project) => (
        <ProjectCard
          key={project.id}
          project={project.id in savedRules ? { ...project, rules: savedRules[project.id] } : project}
          onSaved={(saved) => setSavedRules((prev) => ({ ...prev, [saved.id]: saved.rules }))}
        />
      ))}
    </ul>
  );
}

function ProjectCard({ project, onSaved }: { project: ProjectSummary; onSaved: (p: ProjectSummary) => void }) {
  return (
    <li className="min-w-0 rounded-xl border border-border p-4">
      <h2 className="font-semibold break-words">{project.name}</h2>
      <RulesEditor project={project} onSaved={onSaved} />
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">Runs</h3>
        <StartRunButton projectId={project.id} />
      </div>
      {project.runs.length === 0 ? (
        <p className="mt-1 text-sm text-muted-foreground">No runs yet.</p>
      ) : (
        <ul className="mt-2 divide-y divide-border">
          {project.runs.map((run) => (
            <li key={run.id}>
              <Link
                href={`/runs/${encodeURIComponent(run.id)}`}
                className="-mx-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg px-2 py-2 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
              >
                <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", STATUS_TONE[run.status])}>
                  {runStatusLabel(run.status)}
                </span>
                <span title="Held-out F1: how well the agent did on test cases it never trained on">
                  Score on unseen tests: <strong>{formatScore(run.heldout_f1)}</strong>
                </span>
                <span className="text-muted-foreground">Cost {formatCost(run)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function RulesEditor({ project, onSaved }: { project: ProjectSummary; onSaved: (p: ProjectSummary) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = `rules-${project.id}`;

  async function save() {
    const rules = parseRules(text);
    const problem = rulesProblem(rules);
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      onSaved(await updateProjectRules(project.id, { rules }));
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "We couldn't save your rules.");
    } finally {
      setSaving(false);
    }
  }

  if (!editing) {
    return (
      <div className="mt-2">
        {project.rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">No rules yet.</p>
        ) : (
          <ul className="list-disc space-y-0.5 pl-5 text-sm text-muted-foreground">
            {project.rules.map((rule, i) => (
              <li key={i} className="break-words">
                {rule}
              </li>
            ))}
          </ul>
        )}
        <button
          type="button"
          onClick={() => {
            setText(rulesToText(project.rules));
            setError(null);
            setEditing(true);
          }}
          className={cn(outlineButton, "mt-2")}
        >
          Edit rules
        </button>
      </div>
    );
  }

  return (
    <div className="mt-2">
      <label htmlFor={fieldId} className="text-sm font-medium">
        Rules, one sentence per line
      </label>
      <textarea
        id={fieldId}
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        className="mt-1 w-full rounded-lg border border-border bg-background p-3 text-sm"
      />
      {error && (
        <p role="alert" className="mt-1 text-sm text-red-600">
          {error}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" onClick={save} disabled={saving} className={outlineButton}>
          {saving && <Loader2 className="size-4 animate-spin" aria-hidden />} Save rules
        </button>
        <button type="button" onClick={() => setEditing(false)} disabled={saving} className={outlineButton}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Start another run of this project, then go answer its questions. Quota refusals come back in plain English. */
function StartRunButton({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      const run = await startRun(projectId);
      router.push(`/runs/${encodeURIComponent(run.id)}/intake`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "We couldn't start a run.");
      setStarting(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button type="button" onClick={start} disabled={starting} className={outlineButton}>
        {starting ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Play className="size-4" aria-hidden />} Start a new run
      </button>
      {error && (
        <p role="alert" className="max-w-xs text-right text-sm text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}
