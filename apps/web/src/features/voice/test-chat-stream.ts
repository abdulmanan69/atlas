/**
 * Typed test chats: read the API's server-sent events (`data: <TestChatEvent>` blocks)
 * from a streamed response body. Comment lines (": keep-alive") are skipped.
 */
import type { ChatEvent } from "@/features/voice/test-transcript-model";

function parseBlock(block: string): ChatEvent | null {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""));
  if (data.length === 0) return null;
  try {
    const event = JSON.parse(data.join("\n")) as unknown;
    return event && typeof event === "object" && typeof (event as ChatEvent).type === "string" ? (event as ChatEvent) : null;
  } catch {
    return null;
  }
}

/** Events in arrival order. Aborting `signal` cancels the stream (closing the connection). */
export async function* readChatEvents(body: ReadableStream<Uint8Array>, signal?: AbortSignal): AsyncGenerator<ChatEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => void reader.cancel().catch(() => undefined);
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel);
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = done ? "" : (blocks.pop() ?? "");
      for (const block of blocks) {
        const event = parseBlock(block);
        if (event) yield event;
      }
      if (done) return;
    }
  } finally {
    signal?.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}
