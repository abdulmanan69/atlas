import { useMutation } from "@tanstack/react-query";
import { PhoneCall, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Label } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useAgents, useInvalidate, useLeads } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { fullName } from "@/lib/format";
import { useDebounced } from "@/lib/hooks";
import { useHasRole } from "@/lib/session";

/**
 * Start one call to one lead with one agent. Simulated by default: nothing leaves the
 * platform. A live call is offered only to admins and owners, and needs live calling
 * enabled on the deployment (the API refuses it otherwise).
 */
export function CallNowDialog({
  open,
  onOpenChange,
  defaultLead,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Preselect a lead (for example from the lead's page). */
  defaultLead?: { id: string; label: string } | null;
}) {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const canLaunchLive = useHasRole("admin");
  const [leadId, setLeadId] = useState<string | null>(null);
  const [leadLabel, setLeadLabel] = useState<string | undefined>(undefined);
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim(), 200);
  const [agentId, setAgentId] = useState("");
  const [live, setLive] = useState(false);

  const leads = useLeads({ q: q || undefined, page_size: 12, sort: "-updated_at" });
  const agents = useAgents();
  const ready = useMemo(() => (agents.data ?? []).filter((a) => a.status === "active" && a.published_version !== null), [agents.data]);

  const start = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/calls", { body: { lead_id: leadId!, agent_id: agentId, live } })),
    onSuccess: (call) => {
      toast.success(live ? "Live call started" : "Simulated call started");
      void invalidate("calls");
      onOpenChange(false);
      void navigate(`/calls/${call.id}`);
    },
  });
  const resetError = start.reset;

  useEffect(() => {
    if (!open) return;
    setLeadId(defaultLead?.id ?? null);
    setLeadLabel(defaultLead?.label);
    setSearch("");
    setLive(false);
    resetError();
  }, [open, defaultLead?.id, defaultLead?.label, resetError]);

  useEffect(() => {
    if (!agentId && ready.length === 1) setAgentId(ready[0]!.id);
  }, [ready, agentId]);

  const options = (leads.data?.items ?? []).map((l) => ({
    value: l.id,
    label: fullName(l.first_name, l.last_name),
    description: [l.company, l.do_not_call ? "Do not call" : (l.phone_display ?? "No phone")].filter(Boolean).join(" · "),
    disabled: l.do_not_call || !l.phone_e164,
  }));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Call now"
      description="One AI agent calls one lead. The call appears in the history immediately and updates as it progresses."
      width="max-w-md"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!leadId || !agentId} loading={start.isPending} onClick={() => start.mutate()}>
            <PhoneCall /> {live ? "Place live call" : "Start simulated call"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {start.error ? <FormError message={errorMessage(start.error)} /> : null}
        <Field label="Lead" required hint="Leads marked do-not-call or without a phone number can't be called.">
          <Combobox
            value={leadId}
            onValueChange={(v) => {
              setLeadId(v);
              setLeadLabel(options.find((o) => o.value === v)?.label);
            }}
            options={options}
            valueLabel={leadLabel}
            placeholder="Search leads"
            searchPlaceholder="Name, company, email or phone"
            emptyText={q ? "No leads match." : "Type to search your leads."}
            loading={leads.isFetching}
            onSearchChange={setSearch}
            allowClear
          />
        </Field>
        <Field
          label="Agent"
          required
          hint={
            agents.data && ready.length === 0 ? (
              <>
                No agent is published and active.{" "}
                <Link to="/agents" className="font-medium text-accent hover:underline">
                  Open agents
                </Link>
              </>
            ) : (
              "Only published, active agents can take calls."
            )
          }
        >
          <Select value={agentId} onValueChange={setAgentId} placeholder="Choose an agent" disabled={ready.length === 0}>
            {ready.map((a) => (
              <SelectItem key={a.id} value={a.id} description={`v${a.published_version} · ${a.direction}`}>
                {a.name}
              </SelectItem>
            ))}
          </Select>
        </Field>
        {canLaunchLive ? (
          <div className="rounded-md border border-line bg-surface-2/50 p-3">
            <label className="flex items-start justify-between gap-3">
              <div>
                <Label>Live call</Label>
                <p className="mt-0.5 text-[12px] text-muted">
                  {live
                    ? "A real phone call is placed to the lead's number, and the conversation is recorded."
                    : "Off: a simulated dry run. No phone call is placed; the result is synthetic and labelled as such."}
                </p>
              </div>
              <Switch aria-label="Live call" checked={live} onCheckedChange={setLive} />
            </label>
            {live ? (
              <p className="mt-2 flex items-start gap-1.5 text-[12px] text-warn">
                <ShieldAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                Live calling must be enabled on this deployment; the person's consent and do-not-call status are checked before dialing.
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-[12px] text-muted">Calls started here are simulated. Live calls can be placed by administrators.</p>
        )}
      </div>
    </Dialog>
  );
}
