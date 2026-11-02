// TypeScript port of the synthetic collections agent [Spec §2, §12; I07]. No model key needed.
// Same fixed parsing rules and faulty modes as collections_demo/agent.py, so the same 20-case suite
// grades it from independent simulator evidence, never from what the agent says.

import {
  type InvocationContext,
  type Message,
  type Reply,
  SusanooAgent,
  ToolClient,
  ToolTransportError,
} from "../../../packages/sdk-typescript/src/index.ts";

export const MODES = [
  "correct",
  "false_completion",
  "wrong_amount",
  "wrong_account",
  "duplicate",
  "forbidden_change",
  "handoff",
  "timeout_after_write",
  "lost_ack",
] as const;
export type Mode = (typeof MODES)[number];

const ACCOUNT = /\bACC-\d{3,6}\b/;
const AMOUNT = /(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*(?:rupees|inr)/i;
const DATE = /\b(\d{4}-\d{2}-\d{2})\b/;
const WAIVE = /\bwaive|\bremove (?:the )?(?:late )?fees?\b/i;
const HUMAN = /\bdispute\b|\bnot my (?:loan|account)\b|\bspeak to (?:a )?(?:person|human|agent)\b/i;
const CONFIRM = /\bconfirm\b/i;
const REJECTION_HANDOFF = new Set(["amount_out_of_policy", "date_out_of_policy", "account_not_eligible", "currency_mismatch"]);

/** Exact decimal to minor units without floating point; null for more than two decimals. */
export function toMinor(text: string): number | null {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text.replaceAll(",", ""));
  if (!match) return null;
  const whole = match[1] ?? "0";
  const fraction = (match[2] ?? "").padEnd(2, "0");
  return Number(whole) * 100 + Number(fraction);
}

/** Python's ``f"{Decimal(minor) / 100:,.2f}"``. */
export function formatMinor(minor: number): string {
  const whole = Math.floor(minor / 100).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${whole}.${(minor % 100).toString().padStart(2, "0")}`;
}

interface Conversation {
  accountId: string | null;
  amountMinor: number | null;
  dueDate: string | null;
  promiseId: string | null;
  handedOff: boolean;
}

const newConversation = (): Conversation => ({
  accountId: null,
  amountMinor: null,
  dueDate: null,
  promiseId: null,
  handedOff: false,
});

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason);
    });
  });
}

export class CollectionsAgent extends SusanooAgent {
  override name = "collections-demo-ts";
  override version = "1.0";
  readonly mode: Mode;
  readonly hangSeconds: number;
  readonly sessions = new Map<string, Conversation>();

  constructor(mode: Mode = "correct", hangSeconds = 3600) {
    super();
    if (!(MODES as readonly string[]).includes(mode)) throw new Error(`Unknown mode ${mode}`);
    this.mode = mode;
    this.hangSeconds = hangSeconds;
  }

  override async startSession(context: InvocationContext): Promise<string> {
    const sessionId = await super.startSession(context);
    this.sessions.set(sessionId, newConversation());
    return sessionId;
  }

  override async closeSession(sessionId: string): Promise<void> {
    this.sessions.delete(sessionId);
  }

  async respond(sessionId: string, message: Message, context: InvocationContext, signal: AbortSignal): Promise<Reply> {
    let convo = this.sessions.get(sessionId);
    if (!convo) {
      convo = newConversation();
      this.sessions.set(sessionId, convo);
    }
    this.absorb(convo, message.text);
    if (context.tool_route === null) return { text: "I can't reach the account system right now." };
    const tools = new ToolClient(context.tool_route, { signal });
    const text = await this.act(convo, message.text, tools, context, signal);
    return { text, tool_events: tools.events };
  }

  private absorb(convo: Conversation, text: string): void {
    const account = ACCOUNT.exec(text);
    if (account) convo.accountId = account[0];
    const amount = AMOUNT.exec(text);
    if (amount) convo.amountMinor = toMinor(amount[1] ?? amount[2] ?? "");
    const date = DATE.exec(text);
    if (date) convo.dueDate = date[1] ?? null;
  }

  private async act(
    convo: Conversation,
    text: string,
    tools: ToolClient,
    ctx: InvocationContext,
    signal: AbortSignal,
  ): Promise<string> {
    if (convo.accountId === null) return "Could you share your account number? It starts with ACC-.";
    if (this.mode === "handoff") {
      return this.handoff(convo, tools, "customer_request", "I've asked a specialist to call you back.");
    }
    if (HUMAN.test(text)) return this.handoff(convo, tools, "dispute", "I've passed this to a specialist who will call you.");
    if (WAIVE.test(text)) {
      if (this.mode === "forbidden_change") {
        await tools.call("waive_fees", { account_id: convo.accountId });
        return "I've waived your late fees.";
      }
      if (this.mode === "false_completion") return "Done, your fees are waived.";
      return this.handoff(
        convo,
        tools,
        "out_of_policy",
        "I can't waive fees myself, so I've asked a specialist to review it.",
      );
    }
    if (convo.promiseId && CONFIRM.test(text) && this.mode !== "duplicate" && this.mode !== "lost_ack") {
      return `Your promise ${convo.promiseId} is already recorded.`;
    }
    if (convo.amountMinor === null) return "How much can you pay?";
    if (convo.dueDate === null) return "On what date can you make the payment? Please use YYYY-MM-DD.";
    if (this.mode === "false_completion") {
      return `Done! I've recorded your promise to pay ₹${formatMinor(convo.amountMinor)} on ${convo.dueDate}.`;
    }
    return this.promise(convo, tools, ctx, signal);
  }

  private async promise(convo: Conversation, tools: ToolClient, ctx: InvocationContext, signal: AbortSignal): Promise<string> {
    const account = await tools.call("read_account", { account_id: convo.accountId });
    if (account.status === "rejected") {
      return this.handoff(convo, tools, "other", "I couldn't find that account, so a specialist will help.");
    }
    const info = account.response.account as {
      balance: { currency: string };
      timezone: string;
    };
    let accountId = convo.accountId as string;
    let amount = convo.amountMinor as number;
    const dueDate = convo.dueDate as string;
    if (this.mode === "wrong_account") {
      const cut = accountId.lastIndexOf("-");
      const digits = accountId.slice(cut + 1);
      accountId = `${accountId.slice(0, cut)}-${String(Number(digits) + 1).padStart(digits.length, "0")}`;
    }
    if (this.mode === "wrong_amount") amount = Math.floor(amount / 10);
    const request = {
      account_id: accountId,
      amount: { amount_minor: amount, currency: info.balance.currency },
      due_date: { date: dueDate, timezone: info.timezone },
    };
    const safeKey = `${ctx.attempt_id}:${accountId}:${amount}:${dueDate}`;
    const key = this.mode === "duplicate" || this.mode === "lost_ack" ? null : safeKey;
    let result;
    try {
      result = await tools.call("create_promise", request, key);
    } catch (error) {
      if (!(error instanceof ToolTransportError)) throw error;
      // A lost acknowledgement may still have written. Retry only with the same idempotency key.
      try {
        result = await tools.call("create_promise", request, key);
      } catch (retryError) {
        if (!(retryError instanceof ToolTransportError)) throw retryError;
        return "I couldn't confirm the promise because the system didn't respond. Please call back.";
      }
    }
    if (this.mode === "duplicate") await tools.call("create_promise", request, null);
    if (result.status === "ok" || result.status === "replayed") {
      convo.promiseId = (result.response.promise as { id: string }).id;
      if (this.mode === "forbidden_change") await tools.call("waive_fees", { account_id: accountId });
      if (this.mode === "timeout_after_write") await sleep(this.hangSeconds * 1000, signal);
      return `Thank you. Your promise to pay ₹${formatMinor(convo.amountMinor as number)} on ${dueDate} is recorded as ${convo.promiseId}.`;
    }
    if (result.code !== null && REJECTION_HANDOFF.has(result.code)) {
      return this.handoff(
        convo,
        tools,
        "out_of_policy",
        "That falls outside what I can agree, so I've asked a specialist to call.",
      );
    }
    return "I couldn't record that promise. A specialist will follow up.";
  }

  private async handoff(convo: Conversation, tools: ToolClient, reason: string, text: string): Promise<string> {
    if (!convo.handedOff) {
      const result = await tools.call("request_handoff", { account_id: convo.accountId, reason });
      convo.handedOff = result.status === "ok" || result.status === "replayed";
    }
    return text;
  }
}
