// Fixture agent for subprocess tests: prints diagnostics that must not reach the protocol channel.
import { type InvocationContext, type Message, type Reply, SusanooAgent, serve } from "../../src/index.ts";

class Echo extends SusanooAgent {
  async respond(_sessionId: string, message: Message, _context: InvocationContext, signal: AbortSignal): Promise<Reply> {
    console.log("diagnostic via console.log");
    process.stdout.write("stray stdout write\n");
    if (message.text === "env") return { text: process.env.SUSANOO_RUNNER_TOKEN ?? "absent" };
    if (message.text === "hang") await new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason)));
    return { text: `echo: ${message.text}` };
  }
}

await serve(new Echo());
