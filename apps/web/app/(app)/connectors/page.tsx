import { ConnectorList } from "@/components/app/connector-form";

export default function Page() {
  return (
    <main className="mx-auto w-full max-w-4xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">Connectors</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        Connect the accounts your agents may use. Credentials are stored encrypted, never shown again, and you can revoke
        them at any time.
      </p>
      <ConnectorList />
    </main>
  );
}
