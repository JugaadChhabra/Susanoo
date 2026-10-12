"use client";

import { useState } from "react";
import { Check, Pencil, X } from "lucide-react";
import { isValidEdit, MAX_TITLE_CHARS, type ScenarioDecision } from "@/lib/intake";
import { cn } from "@/lib/utils";
import type { ScenarioCardOut } from "@/types/generated";

const BTN =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm font-medium hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** One example scenario in plain English: approve, edit or reject. */
export function ScenarioCard({
  card,
  decision,
  onChange,
}: {
  card: ScenarioCardOut;
  decision: ScenarioDecision;
  onChange: (d: ScenarioDecision) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(card.title);

  const shown = decision.action === "edit" ? decision.title : card.title;
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
            <span className="font-medium">What happens, and what your agent should do</span>
            <textarea
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              rows={3}
              maxLength={MAX_TITLE_CHARS}
              className="mt-1 w-full rounded-lg border border-border bg-background p-2"
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!isValidEdit(title)}
              onClick={() => {
                onChange({ action: "edit", title });
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
          <p className="text-sm break-words">{shown}</p>
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
                setTitle(shown);
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
