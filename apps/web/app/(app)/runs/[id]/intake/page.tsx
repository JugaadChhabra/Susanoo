import { Suspense } from "react";
import { IntakeFlow } from "@/components/app/intake-flow";

async function IntakeBody({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <IntakeFlow runId={id} />;
}

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">A few quick questions</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        Your answers and approvals decide what Susanoo builds, so it matches what you meant.
      </p>
      <Suspense fallback={<div className="h-64 animate-pulse rounded-2xl bg-slate-900" aria-busy="true" />}>
        <IntakeBody params={params} />
      </Suspense>
    </main>
  );
}
