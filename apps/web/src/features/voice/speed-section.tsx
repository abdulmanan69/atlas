/**
 * "Speed" on the AI providers page: one-click fast stacks, a guide to typical provider
 * latency (vendor-stated, not measured by ATLAS), and, for owners and admins, what the
 * voice runtime has measured across the platform.
 */
import { useMutation } from "@tanstack/react-query";
import { Activity, CheckCircle2, Info, Zap } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { formatMs } from "@/features/calls/latency-model";
import type { LatencyGuide, LatencyPreset } from "@/features/calls/latency-types";
import { useInvalidate } from "@/features/queries";
import { SERVICE_LABELS, useLatencyGuide, usePipelineHealth, type Catalog, type Connection } from "@/features/voice/queries";
import { choiceLabel, missingFromError, missingProviders, providerTitle } from "@/features/voice/speed-model";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";


export function SpeedCard({ catalog, connections, onAddConnection }: { catalog: Catalog | undefined; connections: Connection[] | undefined; onAddConnection: () => void }) {
  const canEdit = useHasRole("admin");
  const guide = useLatencyGuide();

  return (
    <Card>
      <CardHeader title="Speed" description="How quickly agents answer depends mostly on these three choices. Start from a fast stack, then check real calls under Reports." />
      {guide.isError ? (
        <ErrorState error={guide.error} onRetry={() => void guide.refetch()} />
      ) : !guide.data ? (
        <div className="grid gap-3 p-4 md:grid-cols-2">
          <Skeleton className="h-32" />
          <Skeleton className="h-32" />
        </div>
      ) : (
        <>
          {guide.data.presets.length ? (
            <ul className="grid gap-3 p-4 md:grid-cols-2" aria-label="Fast stacks">
              {guide.data.presets.map((p) => (
                <PresetCard key={p.id} preset={p} catalog={catalog} connections={connections} canEdit={canEdit} onAddConnection={onAddConnection} />
              ))}
            </ul>
          ) : null}
          <LatencyGuideTable guide={guide.data} catalog={catalog} />
        </>
      )}
    </Card>
  );
}

function PresetCard({
  preset,
  catalog,
  connections,
  canEdit,
  onAddConnection,
}: {
  preset: LatencyPreset;
  catalog: Catalog | undefined;
  connections: Connection[] | undefined;
  canEdit: boolean;
  onAddConnection: () => void;
}) {
  const invalidate = useInvalidate();
  const [missing, setMissing] = useState<string[] | null>(null);
  const notConnected = missingProviders(preset, connections);
  const apply = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/ai-settings/presets/{preset_id}/apply", { params: { path: { preset_id: preset.id } } })),
    onSuccess: () => {
      setMissing(null);
      toast.success(`${preset.title} applied. New calls use it right away.`);
      void invalidate("ai");
    },
    onError: (e) => {
      // 409: error.details.missing lists the provider ids to connect first.
      const list = e instanceof ApiError && e.status === 409 ? missingFromError(e.details) : null;
      if (list) setMissing(list);
      else toast.error(errorMessage(e));
    },
  });
  const shown = missing ?? (notConnected.length ? notConnected : null);

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-line p-3.5">
      <div className="flex items-start gap-2.5">
        <span className="grid size-7 shrink-0 place-items-center rounded-md bg-accent-soft text-accent-soft-ink">
          <Zap className="size-3.5" aria-hidden />
        </span>
        <div className="min-w-0">
          <h3 className="text-[13.5px] font-semibold">{preset.title}</h3>
          <p className="mt-0.5 text-[12.5px] text-muted">{preset.description}</p>
        </div>
      </div>
      <dl className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[12px]">
        {(["stt", "llm", "tts"] as const).map((k) => (
          <div key={k} className="contents">
            <dt className="text-muted">{SERVICE_LABELS[k].title}</dt>
            <dd className="truncate text-[12px] text-ink-2">{choiceLabel(preset.choices[k], catalog)}</dd>
          </div>
        ))}
      </dl>
      {shown ? (
        <div role={missing ? "alert" : undefined} className="rounded-md bg-warn-soft px-2.5 py-2 text-[12px] text-warn">
          <p>
            {missing ? "Add these connections first: " : "Needs a connection for "}
            {shown.map((id) => providerTitle(id, catalog)).join(", ")}.
          </p>
          {canEdit ? (
            <Button variant="link" size="sm" className="mt-1 text-[12px]" onClick={onAddConnection}>
              Add connection
            </Button>
          ) : null}
        </div>
      ) : (
        <p className="inline-flex items-center gap-1.5 text-[12px] text-success">
          <CheckCircle2 className="size-3.5" aria-hidden /> Connections ready
        </p>
      )}
      {canEdit ? (
        <Button size="sm" className="self-start" loading={apply.isPending} onClick={() => apply.mutate()}>
          Apply
        </Button>
      ) : null}
    </li>
  );
}

function LatencyGuideTable({ guide, catalog }: { guide: LatencyGuide; catalog: Catalog | undefined }) {
  if (!guide.providers.length) return <p className="border-t border-line px-4 py-3 text-[12.5px] text-muted">{guide.disclaimer}</p>;
  return (
    <div className="border-t border-line">
      <h3 className="px-4 pb-1 pt-3 text-[13px] font-semibold">Latency guide</h3>
      <Table aria-label="Latency guide">
        <thead>
          <tr>
            <Th>Service</Th>
            <Th>Provider</Th>
            <Th className="text-right">Typical first response</Th>
            <Th>Streams</Th>
            <Th>Knows when you finish</Th>
            <Th>Note</Th>
          </tr>
        </thead>
        <tbody>
          {guide.providers.map((p, i) => (
            <Tr key={`${p.kind}-${p.provider}-${i}`}>
              <Td className="whitespace-nowrap text-muted">{SERVICE_LABELS[p.kind].title}</Td>
              <Td>
                <span className="font-medium">{providerTitle(p.provider, catalog)}</span>
                {p.model_hint ? <span className="block font-mono text-[11px] text-faint">{p.model_hint}</span> : null}
              </Td>
              <Td className={cn("whitespace-nowrap text-right font-mono", p.typical_ms === null && "text-faint")}>
                {p.typical_ms === null ? "—" : `≈ ${formatMs(p.typical_ms)}`}
                {p.measured ? <span className="block font-sans text-[11px] text-faint">measured by ATLAS</span> : null}
              </Td>
              <Td>{p.streaming ? <Badge tone="success">Yes</Badge> : <Badge>No</Badge>}</Td>
              <Td>{p.kind === "stt" ? p.turn_aware ? <Badge tone="success">Yes</Badge> : <Badge>No</Badge> : <span className="text-faint">—</span>}</Td>
              <Td className="text-[12px] text-muted">{p.note}</Td>
            </Tr>
          ))}
        </tbody>
      </Table>
      <p className="flex items-start gap-1.5 px-4 py-3 text-[12px] text-muted">
        <Info className="mt-0.5 size-3.5 shrink-0 text-faint" aria-hidden />
        <span>{guide.disclaimer}</span>
      </p>
    </div>
  );
}

/** Owner/admin only: the runtime's own histograms, shared by every organization on this platform. */
export function PipelineHealthCard() {
  const allowed = useHasRole("admin");
  const health = usePipelineHealth();
  if (!allowed) return null;
  const data = health.data;
  return (
    <Card>
      <CardHeader
        title="Pipeline health"
        description={`Measured by the voice service for every organization on this platform · ${data?.window ?? "process lifetime"}`}
        action={<Badge>Platform-wide</Badge>}
      />
      {health.isError ? (
        <ErrorState error={health.error} onRetry={() => void health.refetch()} />
      ) : !data ? (
        <div className="p-4">
          <Skeleton className="h-24" />
        </div>
      ) : !data.available ? (
        <EmptyState icon={<Activity />} title="Not available" description={data.note || "The voice service is not reporting metrics."} className="py-8" />
      ) : (
        <>
          <dl className="divide-y divide-line text-[12.5px]">
            {data.response ? (
              <div className="grid grid-cols-[minmax(0,1fr)_64px_64px] items-baseline gap-2 bg-surface-2/50 px-4 py-2.5">
                <dt className="font-medium text-ink">Response (person quiet → agent speaks)</dt>
                <dd className="text-right font-mono">{formatMs(data.response.p50_ms)}</dd>
                <dd className="text-right font-mono">{formatMs(data.response.p95_ms)}</dd>
              </div>
            ) : null}
            <div className="grid grid-cols-[minmax(0,1fr)_64px_64px] gap-2 px-4 py-1.5 text-[10.5px] uppercase tracking-wider text-faint" aria-hidden>
              <span>Service</span>
              <span className="text-right">Median</span>
              <span className="text-right">p95</span>
            </div>
            {(data.services ?? []).map((s) => (
              <div key={s.service} className="grid grid-cols-[minmax(0,1fr)_64px_64px] items-baseline gap-2 px-4 py-2">
                <dt className="min-w-0 truncate">
                  {s.service}
                  <span className="ml-1.5 font-mono text-[11px] text-faint">{s.samples.toLocaleString()} samples</span>
                </dt>
                <dd className="text-right font-mono">{formatMs(s.p50_ms)}</dd>
                <dd className="text-right font-mono">{formatMs(s.p95_ms)}</dd>
              </div>
            ))}
          </dl>
          {data.note ? <p className="border-t border-line px-4 py-2.5 text-[12px] text-muted">{data.note}</p> : null}
        </>
      )}
    </Card>
  );
}
