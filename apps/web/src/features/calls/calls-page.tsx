import { CalendarRange, ChevronDown, PhoneCall, PhoneOff, Search, X } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { EmptyState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { DateTimePicker } from "@/components/ui/date-time-picker";
import { MenuContent, MenuItem, MenuLabel, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Select, SelectItem } from "@/components/ui/select";
import { CallNowDialog } from "@/features/calls/call-now-dialog";
import { CallsGrid, type CallsGridHandle } from "@/features/calls/calls-grid";
import {
  CALL_STATES,
  DATE_PRESETS,
  filtersActive,
  outcomeLabel,
  presetRange,
  readCallsUrl,
  STATE_BADGE,
  urlToFilters,
  type ModeChoice,
} from "@/features/calls/calls-model";
import { useAgentNames, useAgents, useCallsOverview, useCampaigns } from "@/features/queries";
import { useHasRole } from "@/lib/session";

export default function CallsPage() {
  const [params, setParams] = useSearchParams();
  const canCall = useHasRole("manager");
  const [calling, setCalling] = useState(false);
  const grid = useRef<CallsGridHandle>(null);

  const url = useMemo(() => readCallsUrl(params), [params]);
  const filters = useMemo(() => urlToFilters(url), [url]);
  const active = filtersActive(url);

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || (k === "mode" && v === "all")) next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  const agents = useAgents(true);
  const names = useAgentNames();
  const campaigns = useCampaigns({ page_size: 100 });
  // The outcome vocabulary is whatever agents actually produced this year.
  const vocabulary = useCallsOverview({ range_days: 365 });
  const outcomes = useMemo(() => {
    const seen = new Set<string>();
    for (const o of vocabulary.data?.outcomes ?? []) if (o.outcome) seen.add(o.outcome);
    if (url.outcome) seen.add(url.outcome);
    return [...seen].sort();
  }, [vocabulary.data, url.outcome]);

  const agentOptions = useMemo(() => (agents.data ?? []).filter((a) => a.status !== "archived" || a.id === url.agent), [agents.data, url.agent]);

  return (
    <>
      <PageHeader
        title="Calls"
        description="Every conversation your agents had or tried to have, newest first."
        actions={
          canCall ? (
            <Button variant="primary" onClick={() => setCalling(true)}>
              <PhoneCall /> Call now
            </Button>
          ) : null
        }
      />

      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <div className="flex items-center gap-1.5">
            <DateTimePicker aria-label="From" size="sm" value={url.from} onChange={(v) => update({ from: v })} placeholder="From" presets={false} clearable className="w-44" />
            <span className="text-[12px] text-faint">to</span>
            <DateTimePicker aria-label="To" size="sm" value={url.to} onChange={(v) => update({ to: v })} placeholder="Now" presets={false} clearable className="w-44" />
            <MenuRoot>
              <MenuTrigger asChild>
                <Button size="sm" variant="ghost" aria-label="Date presets">
                  <CalendarRange /> <ChevronDown />
                </Button>
              </MenuTrigger>
              <MenuContent align="start">
                <MenuLabel>Quick ranges</MenuLabel>
                {DATE_PRESETS.map((p) => (
                  <MenuItem
                    key={p.id}
                    onSelect={() => {
                      const r = presetRange(p.id);
                      update({ from: r.from, to: r.to });
                    }}
                  >
                    {p.label}
                  </MenuItem>
                ))}
              </MenuContent>
            </MenuRoot>
          </div>
          <Select aria-label="Agent" size="sm" value={url.agent ?? ""} onValueChange={(v) => update({ agent: v })} allowEmpty emptyLabel="All agents" className="w-auto min-w-36">
            {agentOptions.map((a) => (
              <SelectItem key={a.id} value={a.id} description={a.status === "archived" ? "Archived" : undefined}>
                {a.name}
              </SelectItem>
            ))}
          </Select>
          <Select aria-label="Campaign" size="sm" value={url.campaign ?? ""} onValueChange={(v) => update({ campaign: v })} allowEmpty emptyLabel="All campaigns" className="w-auto min-w-36">
            {(campaigns.data?.items ?? []).map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.name}
              </SelectItem>
            ))}
          </Select>
          <Select aria-label="State" size="sm" value={url.state ?? ""} onValueChange={(v) => update({ state: v })} allowEmpty emptyLabel="Any state" className="w-auto min-w-32">
            {CALL_STATES.map((s) => (
              <SelectItem key={s} value={s}>
                {STATE_BADGE[s].label}
              </SelectItem>
            ))}
          </Select>
          <Select aria-label="Outcome" size="sm" value={url.outcome ?? ""} onValueChange={(v) => update({ outcome: v })} allowEmpty emptyLabel="Any outcome" className="w-auto min-w-32">
            {outcomes.map((o) => (
              <SelectItem key={o} value={o}>
                {outcomeLabel(o)}
              </SelectItem>
            ))}
          </Select>
          <Select aria-label="Direction" size="sm" value={url.direction ?? ""} onValueChange={(v) => update({ direction: v })} allowEmpty emptyLabel="Any direction" className="w-auto min-w-32">
            <SelectItem value="outbound">Outbound</SelectItem>
            <SelectItem value="inbound">Inbound</SelectItem>
          </Select>
          <Segmented<ModeChoice>
            label="Mode"
            size="sm"
            value={url.mode}
            onChange={(v) => update({ mode: v })}
            options={[
              { value: "all", label: "All" },
              { value: "live", label: "Live" },
              { value: "simulated", label: "Simulated" },
            ]}
          />
          {url.lead ? (
            <Badge tone="accent" className="h-7 gap-1.5 px-2 text-[12.5px]">
              One lead
              <button aria-label="Remove lead filter" onClick={() => update({ lead: null })}>
                <X className="size-3" />
              </button>
            </Badge>
          ) : null}
          {active ? (
            <Button variant="ghost" size="sm" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
              <X /> Clear filters
            </Button>
          ) : null}
        </div>

        <CallsGrid
          ref={grid}
          filters={filters}
          agentNames={names}
          emptyState={
            active ? (
              <EmptyState icon={<Search />} title="No calls match these filters" description="Widen the date range or clear the filters." />
            ) : (
              <EmptyState
                icon={<PhoneOff />}
                title="No calls yet"
                description="Start a call to a lead, or launch a campaign; every attempt is recorded here with its transcript and outcome."
                action={
                  canCall ? (
                    <Button variant="primary" onClick={() => setCalling(true)}>
                      <PhoneCall /> Call now
                    </Button>
                  ) : null
                }
              />
            )
          }
        />
      </Card>

      <CallNowDialog open={calling} onOpenChange={setCalling} />
    </>
  );
}
