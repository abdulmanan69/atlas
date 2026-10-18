import { CheckCircle2, TriangleAlert } from "lucide-react";

import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/primitives";
import { CopyField } from "@/features/developer/copy-button";
import { isLocalUrl, useSetupInstructions, type Provider } from "@/features/telephony/queries";

/** The URLs to paste into the provider console, with the console steps for this deployment. */
export function SetupInstructionsDialog({ provider, onOpenChange }: { provider: Provider | null; onOpenChange: (open: boolean) => void }) {
  const q = useSetupInstructions(provider?.provider ?? null);
  const data = q.data;
  const local = data ? isLocalUrl(data.inbound_webhook_url) : false;
  const urls = data
    ? [
        { label: "Inbound webhook", value: data.inbound_webhook_url, help: "Where the provider sends incoming calls. One URL for every number; the agent is chosen by the called number." },
        data.fallback_webhook_url ? { label: "Fallback webhook", value: data.fallback_webhook_url, help: "Used by the provider if the primary handler fails." } : null,
        data.event_webhook_url ? { label: "Event webhook", value: data.event_webhook_url, help: "Call status events." } : null,
      ].filter((u): u is NonNullable<typeof u> => u !== null)
    : [];

  return (
    <Dialog
      open={provider !== null}
      onOpenChange={onOpenChange}
      title={`Set up ${provider?.name ?? ""}`}
      description={data ? `What to paste into the ${data.title} console so calls reach your agents.` : undefined}
      width="max-w-xl"
      footer={
        <Button variant="primary" onClick={() => onOpenChange(false)}>
          Done
        </Button>
      }
    >
      {q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !data ? (
        <div className="flex flex-col gap-3">
          <Skeleton className="h-9" />
          <Skeleton className="h-9" />
          <Skeleton className="h-24" />
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {local ? (
            <div role="alert" className="flex items-start gap-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
              <TriangleAlert className="mt-px size-4 shrink-0" aria-hidden />
              <span>
                These URLs point at a local address, so providers on the internet can't reach them. Set the voice service's public HTTPS address (
                <code className="font-mono">ATLAS_VOICE_RUNTIME_PUBLIC_URL</code>) and reopen this dialog.
              </span>
            </div>
          ) : data.automatic ? (
            <div className="flex items-start gap-2 rounded-md bg-success-soft px-3 py-2 text-[12.5px] text-success">
              <CheckCircle2 className="mt-px size-4 shrink-0" aria-hidden />
              <span>Once an agent is assigned to a number, the voice service writes these webhooks into your {data.title} account for you. The steps below are only needed if that reports a problem.</span>
            </div>
          ) : null}

          {urls.length ? (
            <section className="flex flex-col gap-3" aria-labelledby="setup-urls">
              <h3 id="setup-urls" className="eyebrow">
                URLs for this deployment
              </h3>
              {urls.map((u) => (
                <div key={u.label} className="flex flex-col gap-1">
                  <span className="text-[12.5px] font-medium text-ink-2">{u.label}</span>
                  <CopyField value={u.value} label={`${u.label} URL`} />
                  <span className="text-[11.5px] text-muted">{u.help}</span>
                </div>
              ))}
            </section>
          ) : null}

          <section aria-labelledby="setup-steps">
            <h3 id="setup-steps" className="eyebrow mb-2">
              In the {data.title} console
            </h3>
            <ol className="flex flex-col gap-2">
              {data.steps.map((s, i) => (
                <li key={s} className="flex gap-3 text-[13px] text-ink-2">
                  <span className="grid size-5 shrink-0 place-items-center rounded bg-surface-2 font-mono text-[10.5px] text-muted">{i + 1}</span>
                  <span className="min-w-0 break-words">{s}</span>
                </li>
              ))}
            </ol>
          </section>

          {data.notes.filter((n) => !local || !/local one/.test(n)).length ? (
            <ul className="flex flex-col gap-1.5 rounded-md bg-surface-2 px-3 py-2 text-[12px] text-muted">
              {data.notes
                .filter((n) => !local || !/local one/.test(n))
                .map((n) => (
                  <li key={n}>{n}</li>
                ))}
            </ul>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
