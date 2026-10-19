import { readChatEvents } from "@/features/voice/test-chat-stream";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
}

async function collect(chunks: string[]) {
  const out = [];
  for await (const e of readChatEvents(streamOf(chunks))) out.push(e);
  return out;
}

describe("readChatEvents", () => {
  it("reads events split across chunks and skips keep-alive comments and junk", async () => {
    const events = await collect([
      ': keep-alive\n\ndata: {"type":"started","at_ms":2,"call_id":"c1"}\n',
      '\ndata: {"type":"agent_text","at_ms":9',
      '00,"text":"Hi"}\r\n\r\ndata: not json\n\n',
      'data: {"type":"done","at_ms":950,"ended":false}',
    ]);
    expect(events).toEqual([
      { type: "started", at_ms: 2, call_id: "c1" },
      { type: "agent_text", at_ms: 900, text: "Hi" },
      { type: "done", at_ms: 950, ended: false },
    ]);
  });
});
