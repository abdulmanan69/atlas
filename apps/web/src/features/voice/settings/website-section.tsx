/**
 * Add to website: a "talk to us" button for the tenant's own site. Operational, so it saves
 * on its own (no publish needed) and applies at once.
 */
import { useMutation } from "@tanstack/react-query";
import { CircleAlert, Plus, RefreshCw, Save, X } from "lucide-react";
import { useId, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Input, Label, Skeleton } from "@/components/ui/primitives";
import { Switch } from "@/components/ui/switch";
import { useInvalidate } from "@/features/queries";
import { CopyButton } from "@/features/developer/copy-button";
import { useWebEmbed, type WebEmbed } from "@/features/voice/settings/agent-settings-api";
import { normalizeDomain } from "@/features/voice/settings/settings-model";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";

export function WebsiteSection({ agentId, canEdit }: { agentId: string; canEdit: boolean }) {
  const embed = useWebEmbed(agentId);
  if (embed.isLoading) return <Skeleton className="h-28" />;
  if (embed.isError || !embed.data) return <p className="text-[12.5px] text-danger">Couldn't load the website widget. {errorMessage(embed.error)}</p>;
  return <WebsiteEditor key={embed.data.key ?? "new"} agentId={agentId} embed={embed.data} canEdit={canEdit} />;
}

function WebsiteEditor({ agentId, embed, canEdit }: { agentId: string; embed: WebEmbed; canEdit: boolean }) {
  const id = useId();
  const invalidate = useInvalidate();
  const [enabled, setEnabled] = useState(embed.enabled);
  const [domains, setDomains] = useState<string[]>(embed.allowed_domains);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  const [confirmRotate, setConfirmRotate] = useState(false);

  const dirty = enabled !== embed.enabled || domains.join() !== embed.allowed_domains.join();
  const save = useMutation({
    mutationFn: () => unwrap(api.PUT("/api/v1/agents/{agent_id}/web-embed", { params: { path: { agent_id: agentId } }, body: { enabled, allowed_domains: domains } })),
    onSuccess: (d) => {
      toast.success(d.enabled ? "Website widget is on. Paste the snippet on your site." : "Website widget is off.");
      void invalidate("agent", agentId, "web-embed");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const rotate = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/agents/{agent_id}/web-embed/rotate", { params: { path: { agent_id: agentId } } })),
    onSuccess: () => {
      setConfirmRotate(false);
      toast.success("New widget key issued. Update the snippet on your sites.");
      void invalidate("agent", agentId, "web-embed");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const add = () => {
    const domain = normalizeDomain(draft);
    if (!domain) return setDraftError("Enter a domain such as example.com or *.example.com");
    if (domains.length >= 20) return setDraftError("Up to 20 sites");
    setDraftError(null);
    setDraft("");
    if (!domains.includes(domain)) setDomains([...domains, domain]);
  };
  const needsDomain = enabled && domains.length === 0;

  return (
    <div className="flex flex-col gap-4">
      {!embed.agent_published ? (
        <p className="flex items-start gap-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden /> Visitors can only talk to a published, active agent. Publish it before turning the widget on for a live site.
        </p>
      ) : null}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-[13px] font-medium text-ink">Show the widget on allowed sites</p>
          <p className="mt-0.5 text-[12.5px] text-muted">Visitors press a button and talk to the published agent with their microphone. Each conversation is recorded under Calls as an inbound call.</p>
        </div>
        <Switch aria-label="Website widget" checked={enabled} onCheckedChange={setEnabled} disabled={!canEdit} className="mt-0.5" />
      </div>

      <div className="flex flex-col gap-2">
        <Label htmlFor={`${id}-domain`}>Allowed sites</Label>
        <div className="flex gap-2">
          <Input
            id={`${id}-domain`}
            placeholder="example.com, *.example.com, localhost:3000"
            value={draft}
            disabled={!canEdit}
            aria-invalid={draftError ? true : undefined}
            aria-describedby={draftError ? `${id}-domain-error` : `${id}-domain-hint`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <Button type="button" onClick={add} disabled={!canEdit || !draft.trim()}>
            <Plus /> Add
          </Button>
        </div>
        {draftError ? (
          <p id={`${id}-domain-error`} role="alert" className="text-[12px] text-danger">
            {draftError}
          </p>
        ) : (
          <p id={`${id}-domain-hint`} className="text-[12px] text-muted">
            Pages on other sites can't start a call. <span className="font-mono">www.</span> is optional; <span className="font-mono">*.</span> covers every subdomain.
          </p>
        )}
        {domains.length ? (
          <ul className="flex flex-wrap gap-1.5" aria-label="Allowed sites">
            {domains.map((d) => (
              <li key={d} className="inline-flex h-6 items-center gap-1 rounded-[4px] border border-line bg-surface-2 px-1.5 font-mono text-[12px] text-ink-2">
                {d}
                <button type="button" aria-label={`Remove ${d}`} disabled={!canEdit} onClick={() => setDomains(domains.filter((x) => x !== d))} className="grid size-4 place-items-center rounded text-faint hover:text-ink">
                  <X className="size-3" />
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="primary" disabled={!canEdit || !dirty || needsDomain} loading={save.isPending} onClick={() => save.mutate()}>
          <Save /> Save widget
        </Button>
        {needsDomain ? <span className="text-[12px] text-danger">Add at least one site to turn the widget on.</span> : dirty ? <span className="text-[12px] text-warn">Unsaved widget changes</span> : <span className="text-[12px] text-muted">Applies at once; no publish needed.</span>}
      </div>

      {embed.snippet && embed.key ? (
        <div className="flex flex-col gap-2 border-t border-line pt-4">
          <div className="flex items-center justify-between gap-2">
            <Label>Snippet</Label>
            <div className="flex items-center gap-1">
              <CopyButton value={embed.snippet} label="Copy snippet" size="sm" variant="secondary">
                Copy
              </CopyButton>
              <Button type="button" size="sm" variant="ghost" disabled={!canEdit} onClick={() => setConfirmRotate(true)}>
                <RefreshCw /> New key
              </Button>
            </div>
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-line-strong bg-surface-2/60 px-3 py-2 font-mono text-[12px] text-ink" aria-label="Widget snippet">
            {embed.snippet}
          </pre>
          <p className="text-[12px] text-muted">
            Paste it before <span className="font-mono">{"</body>"}</span>. Optional: <span className="font-mono">data-label</span> (button text), <span className="font-mono">data-position="left"</span>,{" "}
            <span className="font-mono">data-color</span>.
            {embed.sessions_started ? ` ${embed.sessions_started} conversation${embed.sessions_started === 1 ? "" : "s"} started${embed.last_used_at ? `, last ${formatRelative(embed.last_used_at)}` : ""}.` : " No conversations yet."}
          </p>
          <p className="text-[12px] text-muted">Deployment: visitors' browsers send audio straight to the voice service, so its public address must be reachable from the internet (HTTPS in production).</p>
        </div>
      ) : (
        <p className="text-[12px] text-muted">Turn the widget on and save to get the snippet.</p>
      )}

      <ConfirmDialog
        open={confirmRotate}
        onOpenChange={setConfirmRotate}
        title="Issue a new widget key?"
        description="The snippet on your sites stops working at once. Paste the new snippet wherever the widget appears."
        confirmLabel="Issue new key"
        loading={rotate.isPending}
        onConfirm={() => rotate.mutate()}
      />
    </div>
  );
}
