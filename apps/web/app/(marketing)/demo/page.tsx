import { DemoReplay } from "@/components/app/demo-replay";

export const metadata = { title: "Watch Susanoo work · Demo" };

export default function Page() {
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Watch Susanoo work</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        A replay of a real run: it builds an agent, attacks it, finds out why it failed, fixes it and proves the fix. No sign-in needed.
      </p>
      <DemoReplay autoplay />
    </main>
  );
}
