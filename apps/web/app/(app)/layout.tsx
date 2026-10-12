import Link from "next/link";
import { SignOutButton } from "@/components/auth/sign-out-button";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-stamp stamp-palette dots flex min-h-full flex-1 flex-col">
      <header className="flex items-center justify-between gap-2 border-b-[3px] border-ink px-4 py-3 sm:px-6">
        <nav className="flex items-center gap-5 text-base font-medium tracking-[0.06em] uppercase">
          <Link href="/projects" className="flex items-center gap-3">
            <span
              aria-hidden
              className="vert rounded-[2px] bg-vermilion px-1 py-1.5 font-mincho text-[14px] leading-[1.05] font-extrabold text-paper"
            >
              須佐
            </span>
            <span className="mk-display text-xl tracking-[0.04em]">Susanoo</span>
          </Link>
          <Link href="/connectors" className="hover:text-vermilion">
            Connectors
          </Link>
        </nav>
        <SignOutButton />
      </header>
      {children}
    </div>
  );
}
