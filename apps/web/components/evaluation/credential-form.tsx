"use client";

import { useEffect, useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { platform } from "@/lib/platform-api";

export function CredentialForm({ mode }: { mode: "reset" | "accept" }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    const params = new URLSearchParams(window.location.hash.slice(1));
    // The credential fragment exists only in the browser; remove it before navigation or logging.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToken(params.get("token") ?? "");
    window.history.replaceState(null, "", window.location.pathname);
  }, []);
  return (
    <div className="evaluation">
      <header className="ev-top">
        <div className="ev-brand">
          <ShieldCheck size={30} />
          <h1>Susanoo</h1>
        </div>
      </header>
      <main className="ev-login">
        <h2>{mode === "accept" ? "Accept Invitation" : "Reset Password"}</h2>
        {error && (
          <div className="ev-error" role="alert">
            {error}
          </div>
        )}
        {message ? (
          <div role="status">{message}</div>
        ) : (
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              setBusy(true);
              setError("");
              try {
                if (!token && mode === "reset") {
                  const result = await platform<{ message: string }>(
                    "/auth/password-reset",
                    "POST",
                    { email: form.get("email") },
                  );
                  setMessage(result.message);
                } else if (!token) {
                  setError("An unexpired invitation link is required.");
                } else {
                  await platform(
                    mode === "accept"
                      ? "/invitations/accept"
                      : "/auth/password-reset/confirm",
                    "POST",
                    {
                      token,
                      password: form.get("password"),
                    },
                  );
                  setToken("");
                  setMessage(
                    mode === "accept"
                      ? "Invitation accepted. Sign in to continue."
                      : "Password updated. Sign in again.",
                  );
                }
              } catch (failure) {
                setError((failure as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {!token && mode === "reset" ? (
              <label>
                Email
                <input
                  type="email"
                  name="email"
                  autoComplete="email"
                  required
                />
              </label>
            ) : (
              <label>
                {mode === "accept" ? "Account Password" : "New Password"}
                <input
                  name="password"
                  type="password"
                  minLength={12}
                  maxLength={1024}
                  required
                  autoComplete={
                    mode === "accept" ? "current-password" : "new-password"
                  }
                />
              </label>
            )}
            <button
              className="ev-primary"
              disabled={busy || (mode === "accept" && !token)}
            >
              <KeyRound size={16} />
              {busy ? "Submitting..." : token ? "Confirm" : "Send Reset Link"}
            </button>
          </form>
        )}
        <div className="ev-login-links">
          <a href="/evaluation">Sign In</a>
        </div>
      </main>
    </div>
  );
}
