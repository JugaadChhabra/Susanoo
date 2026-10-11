/** Pure auth helpers shared by the proxy, the callback route and the login page. [PRD › Auth flow] */

/** Top-level paths of the (app) route group. Everything else is public (marketing, demo, login). */
const PROTECTED_PREFIXES = ["/projects", "/connectors", "/runs"] as const;

export const DEFAULT_AFTER_LOGIN = "/projects";

export function isProtectedPath(pathname: string): boolean {
  return PROTECTED_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/** Only same-site paths are allowed as a post-login destination, so `?next=` can't bounce users off-site. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return DEFAULT_AFTER_LOGIN;
  }
  return next;
}

export function loginUrlFor(pathname: string, search = ""): string {
  return `/login?next=${encodeURIComponent(pathname + search)}`;
}
