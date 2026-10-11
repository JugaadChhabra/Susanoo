import { Suspense } from "react";
import { RunView } from "@/components/app/run-view";

async function RunBody({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // In development, `/runs/fixture` replays the bundled fixture run; any other id streams live from the API.
  return <RunView source={id === "fixture" && process.env.NODE_ENV !== "production" ? { kind: "fixture" } : { kind: "live", runId: id }} />;
}

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Your run</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        Watch Susanoo build, attack, fix and re-test your agent.
      </p>
      <Suspense fallback={<div className="h-96 animate-pulse rounded-2xl bg-slate-900" aria-busy="true" />}>
        <RunBody params={params} />
      </Suspense>
    </main>
  );
}
