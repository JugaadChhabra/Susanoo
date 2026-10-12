import { DemoReplay } from "@/components/app/demo-replay";

export const metadata = { title: "Watch Susanoo work · Demo" };

export default function Page() {
  return (
    <div className="app-stamp dots flex min-h-full flex-1 flex-col">
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12">
      <h1 className="mk-display text-3xl sm:text-4xl">Watch Susanoo work</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        A replay of a real run: it builds an agent, attacks it, finds out why it failed, fixes it and proves the fix. No sign-in needed.
      </p>
      <div className="stamp-palette border-2 border-ink bg-ink p-2 sm:p-3">
        <DemoReplay autoplay />
      </div>
    </main>
    </div>
  );
}
