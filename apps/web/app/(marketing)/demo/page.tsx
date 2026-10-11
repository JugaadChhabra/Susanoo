import { RunView } from "@/components/app/run-view";
import { demoSource } from "@/lib/demo";

export const metadata = { title: "Watch Susanoo work · Demo" };

export default function Page() {
  const source = demoSource(process.env.NEXT_PUBLIC_DEMO_RUN_ID, process.env.NODE_ENV === "production");
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Watch Susanoo work</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        A replay of a real run: it builds an agent, attacks it, finds out why it failed, fixes it and proves the fix. No sign-in needed.
      </p>
      {source ? (
        <RunView source={source} />
      ) : (
        <div role="status" className="rounded-2xl bg-slate-900 p-6 text-sm text-slate-300 ring-1 ring-slate-800">
          The demo run isn&apos;t ready yet. Please check back soon.
        </div>
      )}
    </main>
  );
}
