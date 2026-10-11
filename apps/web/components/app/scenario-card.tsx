"use client";

import { useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { isValidEdit, type ScenarioCard as Card, type ScenarioDecision } from "@/lib/intake";
import { cn } from "@/lib/utils";

const BTN =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** One example scenario in plain English: approve, edit or reject. */
export function ScenarioCard({
  card,
  decision,
  onChange,
}: {
  card: Card;
  decision: ScenarioDecision;
  onChange: (d: ScenarioDecision) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [description, setDescription] = useState(card.description);
  const [expected, setExpected] = useState(card.expected);

  const shown = decision.action === "edit" ? decision : card;
  const status =
    decision.action === "approve" ? "Approved" : decision.action === "edit" ? "Edited and approved" : decision.action === "reject" ? "Rejected" : null;

  return (
    <li
      className={cn(
        "rounded-xl border p-4",
        decision.action === "reject" ? "border-border opacity-60" : decision.action === "pending" ? "border-border" : "border-emerald-600/50",
      )}
    >
      {editing ? (
        <div className="space-y-3">
          <label className="block text-sm">
            <span className="font-medium">What happens</span>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-border bg-background p-2" />
          </label>
          <label className="block text-sm">
            <span className="font-medium">What should happen</span>
            <textarea value={expected} onChange={(e) => setExpected(e.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-border bg-background p-2" />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!isValidEdit(description, expected)}
              onClick={() => {
                onChange({ action: "edit", description, expected });
                setEditing(false);
              }}
              className={cn(BTN, "bg-foreground text-background hover:bg-foreground/90 disabled:opacity-50")}
            >
              Save and approve
            </button>
            <button type="button" onClick={() => setEditing(false)} className={BTN}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          <p className="text-sm">{shown.description}</p>
          <p className="mt-1 text-sm text-muted-foreground">
            <span className="font-medium text-foreground">Expected:</span> {shown.expected}
          </p>
          {status && (
            <p role="status" className="mt-2 text-xs font-medium text-muted-foreground">
              {status}
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" aria-pressed={decision.action === "approve"} onClick={() => decision.action !== "edit" && onChange({ action: "approve" })} className={BTN}>
              <Check className="size-4" aria-hidden /> Approve
            </button>
            <button
              type="button"
              onClick={() => {
                setDescription(shown.description);
                setExpected(shown.expected);
                setEditing(true);
              }}
              className={BTN}
            >
              <Pencil className="size-4" aria-hidden /> Edit
            </button>
            <button type="button" aria-pressed={decision.action === "reject"} onClick={() => onChange({ action: "reject" })} className={BTN}>
              <X className="size-4" aria-hidden /> Reject
            </button>
          </div>
        </>
      )}
    </li>
  );
}
