import { IntakeFlow } from "@/components/app/intake-flow";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">A few quick questions</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        Your answers and approvals decide what Susanoo builds, so it matches what you meant.
      </p>
      <IntakeFlow runId={id} />
    </main>
  );
}
