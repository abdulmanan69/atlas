import { useState } from "react";

import { Segmented } from "@/components/ui/segmented";
import { Badge, Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { CopyButton } from "@/features/developer/copy-button";
import { useEventTypes, WILDCARD } from "@/features/developer/queries";

const AUTH_EXAMPLE = `GET /api/v1/leads?page_size=50
X-API-Key: atlas_k3Xw…`;

const PYTHON = `import hmac, hashlib, time

def verify(secret: str, headers: dict, body: bytes, tolerance: int = 300) -> bool:
    timestamp = headers["X-Atlas-Timestamp"]
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers["X-Atlas-Signature"], f"v1={expected}")`;

const NODE = `const crypto = require("node:crypto");

function verify(secret, headers, rawBody, tolerance = 300) {
  const timestamp = headers["x-atlas-timestamp"];
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > tolerance) return false;
  const expected = "v1=" + crypto
    .createHmac("sha256", secret)
    .update(\`\${timestamp}.\`)
    .update(rawBody) // Buffer of the raw request body
    .digest("hex");
  const given = headers["x-atlas-signature"] || "";
  return given.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(given), Buffer.from(expected));
}`;

const REQUEST_EXAMPLE = `POST /atlas HTTP/1.1
Content-Type: application/json
User-Agent: ATLAS-Webhooks/1.0
X-Atlas-Event: lead.stage_changed
X-Atlas-Event-Id: 6d1f9d2e-5c0a-4d5e-9f3b-2c1a0b7e8f90
X-Atlas-Delivery-Id: 0b4c3a2d-…
X-Atlas-Timestamp: 1760745600
X-Atlas-Signature: v1=5f1a…c0de`;

/** What `data` carries per event; lead events never include contact details. */
const PAYLOADS: Record<string, string> = {
  "lead.created": "lead_id, first_name, last_name, company, stage, stage_id, owner_user_id, source, tags, qualification_status, do_not_call",
  "lead.updated": "the lead.created fields plus changed_fields (list of names)",
  "lead.stage_changed": "lead_id, from_stage, to_stage, from_stage_id, to_stage_id",
  "call.started": "call identifiers, lead id, outcome and captured fields",
  "call.completed": "call identifiers, lead id, outcome and captured fields",
  "campaign.started": "campaign identifiers and counts",
  "campaign.completed": "campaign identifiers and counts",
  ping: "endpoint_id, message — sent by the test action",
};

export function DocsTab() {
  const [lang, setLang] = useState<"python" | "node">("python");
  const events = useEventTypes();
  const snippet = lang === "python" ? PYTHON : NODE;

  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <Card className="xl:col-span-2">
        <CardHeader title="Authenticating with an API key" description="Server-to-server only. Never ship a key in a browser or mobile app." />
        <div className="grid gap-5 px-4 py-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <Code value={AUTH_EXAMPLE} label="Request example" />
          <ul className="flex flex-col gap-2 text-[13px] text-ink-2">
            <li>
              Send the key in the <code className="font-mono">X-API-Key</code> header instead of a bearer token.
            </li>
            <li>
              <code className="font-mono">X-Organization-Id</code> isn't needed: the key names its organization. If you send it, it must match, or the request is refused with 403.
            </li>
            <li>
              Keys work for every organization-scoped endpoint, not for <code className="font-mono">/api/v1/auth/*</code> or creating organizations. Every request is checked against the key's role exactly like a user session.
            </li>
            <li>
              <span className="font-medium text-ink">600 requests per minute</span> per key. Above that you get 429 with <code className="font-mono">error.code = "rate_limited"</code> and a <code className="font-mono">retry_after</code> in seconds.
            </li>
            <li>Actions performed with a key appear in the audit log under the member who created it, with the key recorded as the actor.</li>
          </ul>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Verifying webhook signatures"
          description="Every delivery is signed with the endpoint's secret."
          action={
            <Segmented
              label="Language"
              size="sm"
              value={lang}
              onChange={setLang}
              options={[
                { value: "python", label: "Python" },
                { value: "node", label: "Node.js" },
              ]}
            />
          }
        />
        <div className="flex flex-col gap-3 px-4 py-4 text-[13px] text-ink-2">
          <p>
            <code className="font-mono">signature = HMAC-SHA256(secret, "&lt;X-Atlas-Timestamp&gt;.&lt;raw request body&gt;")</code>, sent as <code className="font-mono">X-Atlas-Signature: v1=&lt;hex&gt;</code>.
          </p>
          <Code value={snippet} label={`${lang === "python" ? "Python" : "Node.js"} verification`} />
          <ul className="flex list-disc flex-col gap-1 pl-5 text-[12.5px]">
            <li>Sign the raw bytes you received, never a re-serialized copy. With Express, use <code className="font-mono">express.raw({`{ type: "application/json" }`})</code> on the webhook route.</li>
            <li>Reject timestamps older than five minutes to prevent replays, and compare signatures with a constant-time function.</li>
            <li>Respond with any 2xx within 10 seconds and do your processing afterwards; a slow handler is treated as a failure and retried.</li>
          </ul>
        </div>
      </Card>

      <Card>
        <CardHeader title="What a delivery looks like" description="Headers identify the event; the body carries it." />
        <div className="flex flex-col gap-3 px-4 py-4 text-[12.5px] text-ink-2">
          <Code value={REQUEST_EXAMPLE} label="Delivery headers" />
          <ul className="flex list-disc flex-col gap-1 pl-5">
            <li>
              Delivered <span className="font-medium text-ink">at least once</span>, usually within 15 seconds. Use <code className="font-mono">id</code> (also <code className="font-mono">X-Atlas-Event-Id</code>) as an idempotency key.
            </li>
            <li>
              Order isn't guaranteed across events; use <code className="font-mono">created_at</code> when sequence matters.
            </li>
            <li>Failures are retried with exponential backoff, 8 attempts in about two hours, then marked dead for 30 days and retryable from the delivery history.</li>
            <li>After 50 consecutive failures the endpoint is disabled automatically. Fix the receiver, then switch it back on; the counter resets and parked deliveries resume.</li>
          </ul>
        </div>
      </Card>

      <Card className="xl:col-span-2">
        <CardHeader title="Event catalogue" description="Lead events carry identifiers, pipeline position and name/company only. Phone numbers, email addresses, notes and custom fields are never included; fetch them with an API key." />
        {events.isLoading ? (
          <div className="p-4">
            <Skeleton className="h-24" />
          </div>
        ) : (
          <dl className="divide-y divide-line">
            {(events.data ?? []).map((e) => (
              <div key={e.type} className="grid gap-1 px-4 py-2.5 md:grid-cols-[220px_minmax(0,1fr)] md:gap-4">
                <dt className="flex items-center gap-2">
                  <code className="font-mono text-[12.5px] text-ink">{e.type}</code>
                  {e.type === WILDCARD ? <Badge tone="accent">Subscribe to all</Badge> : null}
                </dt>
                <dd className="text-[12.5px] text-ink-2">
                  {e.description}
                  {PAYLOADS[e.type] ? (
                    <span className="block text-[12px] text-muted">
                      <span className="font-mono">data</span>: {PAYLOADS[e.type]}
                    </span>
                  ) : null}
                </dd>
              </div>
            ))}
            {PAYLOADS.ping && !events.data?.some((e) => e.type === "ping") ? (
              <div className="grid gap-1 px-4 py-2.5 md:grid-cols-[220px_minmax(0,1fr)] md:gap-4">
                <dt>
                  <code className="font-mono text-[12.5px] text-ink">ping</code>
                </dt>
                <dd className="text-[12.5px] text-ink-2">
                  Sent by the test action.
                  <span className="block text-[12px] text-muted">
                    <span className="font-mono">data</span>: {PAYLOADS.ping}
                  </span>
                </dd>
              </div>
            ) : null}
          </dl>
        )}
      </Card>
    </div>
  );
}

function Code({ value, label }: { value: string; label: string }) {
  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-md border border-line bg-surface-2/60 p-3 pr-10 font-mono text-[11.5px] leading-relaxed text-ink-2" aria-label={label}>
        {value}
      </pre>
      <CopyButton value={value} label={`Copy ${label.toLowerCase()}`} className="absolute right-1.5 top-1.5" />
    </div>
  );
}
