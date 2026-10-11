import Link from "next/link";
import { SignOutButton } from "@/components/auth/sign-out-button";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
        <nav className="flex items-center gap-4 text-sm">
          <Link href="/projects" className="font-semibold">
            Susanoo
          </Link>
          <Link href="/connectors" className="text-muted-foreground hover:text-foreground">
            Connectors
          </Link>
        </nav>
        <SignOutButton />
      </header>
      {children}
    </>
  );
}
