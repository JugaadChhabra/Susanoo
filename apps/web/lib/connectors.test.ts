import { describe, expect, it } from "vitest";
import type { ConnectionSummary, ConnectorSpec } from "@/types/generated";
import { connectionLabel, connectionStatusLabel, guideSegments, missingField, writesLabel } from "./connectors";

const spec: ConnectorSpec = {
  id: "github",
  name: "GitHub",
  summary: "Lets your agent read pull requests.",
  fields: [
    { name: "repo", label: "Repository", help: "", secret: false, placeholder: "" },
    { name: "token", label: "Fine-grained personal access token", help: "", secret: true, placeholder: "" },
  ],
  scopes: [],
  actions: [{ name: "post_review_comment", description: "Post the review as a comment", write: true }],
};

const connection = (overrides: Partial<ConnectionSummary> = {}): ConnectionSummary => ({
  id: "c1",
  connector: "github",
  status: "connected",
  writes_enabled: false,
  settings: { repo: "acme/payments-api" },
  ...overrides,
});

describe("connectionStatusLabel", () => {
  it("says each status in plain English", () => {
    expect(connectionStatusLabel("connected")).toBe("Connected");
    expect(connectionStatusLabel("untested")).toBe("Not tested yet");
    expect(connectionStatusLabel("failed")).toBe("Not working");
  });
});

describe("connectionLabel", () => {
  it("names the connector and what it reaches", () => {
    expect(connectionLabel(spec, connection())).toBe("GitHub · acme/payments-api");
    expect(connectionLabel(spec, connection({ settings: {} }))).toBe("GitHub");
  });
});

describe("writesLabel", () => {
  it("makes clear that writes are a dry run until switched on", () => {
    expect(writesLabel(connection())).toMatch(/dry run/i);
    expect(writesLabel(connection({ writes_enabled: true }))).toMatch(/can post/i);
  });
});

describe("missingField", () => {
  it("names the first empty field", () => {
    expect(missingField(spec, { repo: " ", token: "x" })).toBe("Fill in Repository.");
    expect(missingField(spec, { repo: "acme/x" })).toBe("Fill in Fine-grained personal access token.");
    expect(missingField(spec, { repo: "acme/x", token: "x" })).toBeNull();
  });
});

describe("guideSegments", () => {
  it("splits bold text and https links out of a guide step", () => {
    expect(guideSegments("Open **Settings** then [the form](https://github.com/settings/x).")).toEqual([
      { kind: "text", text: "Open " },
      { kind: "bold", text: "Settings" },
      { kind: "text", text: " then " },
      { kind: "link", text: "the form", href: "https://github.com/settings/x" },
      { kind: "text", text: "." },
    ]);
  });

  it("leaves non-https links as plain text", () => {
    expect(guideSegments("[x](javascript:alert(1))")).toEqual([{ kind: "text", text: "[x](javascript:alert(1))" }]);
  });

  it("returns plain text untouched", () => {
    expect(guideSegments("Just text")).toEqual([{ kind: "text", text: "Just text" }]);
  });
});
