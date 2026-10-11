import { describe, expect, it } from "vitest";
import { isProtectedPath, loginUrlFor, safeNext } from "./auth";

describe("isProtectedPath", () => {
  it("protects every (app) route", () => {
    for (const p of ["/projects", "/projects/new", "/connectors", "/runs/abc", "/runs/abc/agent"]) {
      expect(isProtectedPath(p)).toBe(true);
    }
  });
  it("leaves marketing, demo, login and callback public", () => {
    for (const p of ["/", "/pricing", "/demo", "/login", "/auth/callback", "/projectsx"]) {
      expect(isProtectedPath(p)).toBe(false);
    }
  });
});

describe("safeNext", () => {
  it("keeps same-site paths", () => {
    expect(safeNext("/runs/1?tab=log")).toBe("/runs/1?tab=log");
  });
  it("falls back for anything that could leave the site", () => {
    for (const n of [null, undefined, "", "https://evil.test", "//evil.test", "/\\evil.test", "runs"]) {
      expect(safeNext(n)).toBe("/projects");
    }
  });
});

describe("loginUrlFor", () => {
  it("remembers where the user was going", () => {
    expect(loginUrlFor("/runs/1", "?a=b")).toBe("/login?next=%2Fruns%2F1%3Fa%3Db");
  });
});
