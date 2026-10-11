import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { isProtectedPath, loginUrlFor } from "@/lib/auth";

/** Sends logged-out visitors from (app) routes to /login, and keeps the Supabase session cookie fresh. */
export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  let loggedIn = false;
  if (url && key) {
    const supabase = createServerClient(url, key, {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (cookies) => {
          cookies.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookies.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        },
      },
    });
    // getUser asks Supabase to validate the token; getSession alone would trust the cookie.
    const { data } = await supabase.auth.getUser();
    loggedIn = Boolean(data.user);
  }

  // Not configured counts as logged out: protected pages stay closed rather than open.
  if (!loggedIn && isProtectedPath(request.nextUrl.pathname)) {
    const redirect = NextResponse.redirect(
      new URL(loginUrlFor(request.nextUrl.pathname, request.nextUrl.search), request.url),
    );
    response.cookies.getAll().forEach((c) => redirect.cookies.set(c));
    return redirect;
  }
  return response;
}

export const config = {
  matcher: ["/projects/:path*", "/connectors/:path*", "/runs/:path*"],
};
