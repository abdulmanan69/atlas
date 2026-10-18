import { useMutation } from "@tanstack/react-query";
import { ChevronDown, Download, Plus, Search, Upload, Users, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Avatar, Badge, Card, Input } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { ImportDialog } from "@/features/crm/import-dialog";
import { LeadFormDialog } from "@/features/crm/lead-form-dialog";
import { LeadsGrid, type LeadsGridHandle } from "@/features/crm/leads-grid";
import type { LeadFilters, LeadSort } from "@/features/crm/leads-grid-model";
import { useInvalidate, useMemberNames, useMembers, useStages } from "@/features/queries";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";
import { useHasRole, useSession } from "@/lib/session";

export default function LeadsPage() {
  const [params, setParams] = useSearchParams();
  const me = useSession((s) => s.user?.id);
  const canWrite = useHasRole("agent");
  const canManage = useHasRole("manager");
  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const q = useDebounced(searchInput.trim(), 250);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const grid = useRef<LeadsGridHandle>(null);

  const stageId = params.get("stage") ?? "";
  const owner = params.get("owner") ?? "";
  const sort = (params.get("sort") as LeadSort | null) ?? "-created_at";
  const dnc = params.get("dnc");
  const tag = params.get("tag") ?? "";

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  useEffect(() => {
    if ((params.get("q") ?? "") !== q) update({ q: q || null });
  }, [q]);

  const filters = useMemo<LeadFilters>(
    () => ({
      q: q || undefined,
      stage_id: stageId || undefined,
      owner_user_id: owner && owner !== "unassigned" ? (owner === "me" ? me : owner) : undefined,
      unassigned: owner === "unassigned" || undefined,
      do_not_call: dnc === null ? undefined : dnc === "true",
      tag: tag || undefined,
    }),
    [q, stageId, owner, me, dnc, tag],
  );
  const stages = useStages();
  const members = useMembers();
  const names = useMemberNames();
  const filtersActive = Boolean(q || stageId || owner || dnc || tag);

  const exportCsv = useMutation({
    mutationFn: async () => {
      const blob = await unwrap(api.GET("/api/v1/leads/export", { params: { query: filters }, parseAs: "blob" }));
      const url = URL.createObjectURL(blob as Blob);
      const a = Object.assign(document.createElement("a"), { href: url, download: `leads-${new Date().toISOString().slice(0, 10)}.csv` });
      a.click();
      URL.revokeObjectURL(url);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const ownerOptions = useMemo(
    () => [
      { value: "me", label: "Owned by me" },
      { value: "unassigned", label: "Unassigned" },
      ...(members.data ?? [])
        .filter((m) => m.user.id !== me)
        .map((m) => ({ value: m.user.id, label: m.user.full_name, icon: <Avatar name={m.user.full_name} className="size-4 text-[8px]" /> })),
    ],
    [members.data, me],
  );

  return (
    <>
      <PageHeader
        title="Leads"
        description="Everyone your team or your agents can contact, with where they stand."
        actions={
          <>
            {canManage ? (
              <>
                <Button onClick={() => setImporting(true)}>
                  <Upload /> Import
                </Button>
                <Button onClick={() => exportCsv.mutate()} loading={exportCsv.isPending}>
                  <Download /> Export
                </Button>
              </>
            ) : null}
            {canWrite ? (
              <Button variant="primary" onClick={() => setCreating(true)}>
                <Plus /> New lead
              </Button>
            ) : null}
          </>
        }
      />

      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <div className="relative w-full sm:w-72">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-faint" aria-hidden />
            <Input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Name, company, email or phone"
              aria-label="Search leads"
              className="pl-8"
            />
          </div>
          <Select aria-label="Stage" value={stageId} onValueChange={(v) => update({ stage: v })} allowEmpty emptyLabel="All stages" className="w-auto min-w-36">
            {stages.data?.map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.name}
              </SelectItem>
            ))}
          </Select>
          <Combobox
            aria-label="Owner"
            value={owner || null}
            onValueChange={(v) => update({ owner: v })}
            options={ownerOptions}
            placeholder="Any owner"
            searchPlaceholder="Search teammates"
            allowClear
            className="w-44"
          />
          <Select aria-label="Calling" value={dnc ?? ""} onValueChange={(v) => update({ dnc: v })} allowEmpty emptyLabel="Any calling status" className="w-auto min-w-36">
            <SelectItem value="false">Callable</SelectItem>
            <SelectItem value="true">Do not call</SelectItem>
          </Select>
          {tag ? (
            <Badge tone="accent" className="h-7 gap-1.5 px-2 text-[12.5px]">
              Tag: {tag}
              <button aria-label="Remove tag filter" onClick={() => update({ tag: null })}>
                <X className="size-3" />
              </button>
            </Badge>
          ) : null}
          {filtersActive ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setSearchInput("");
                setParams(new URLSearchParams(), { replace: true });
              }}
            >
              <X /> Clear filters
            </Button>
          ) : null}
        </div>

        {selectedIds.length > 0 ? <BulkBar ids={selectedIds} onDone={() => grid.current?.clearSelection()} /> : null}

        <LeadsGrid
          ref={grid}
          filters={filters}
          sort={sort}
          onSortChange={(s) => update({ sort: s })}
          selectable={canWrite}
          onSelectionChange={setSelectedIds}
          stages={stages.data}
          names={names}
          emptyState={
            filtersActive ? (
              <EmptyState icon={<Search />} title="No leads match these filters" description="Try a different search or clear the filters." />
            ) : (
              <EmptyState
                icon={<Users />}
                title="Add your first leads"
                description="Create a lead by hand, or import a CSV with names and phone numbers."
                action={
                  canWrite ? (
                    <div className="flex gap-2">
                      {canManage ? (
                        <Button onClick={() => setImporting(true)}>
                          <Upload /> Import CSV
                        </Button>
                      ) : null}
                      <Button variant="primary" onClick={() => setCreating(true)}>
                        <Plus /> New lead
                      </Button>
                    </div>
                  ) : null
                }
              />
            )
          }
        />
      </Card>

      <LeadFormDialog open={creating} onOpenChange={setCreating} />
      <ImportDialog open={importing} onOpenChange={setImporting} />
    </>
  );
}

function BulkBar({ ids, onDone }: { ids: string[]; onDone: () => void }) {
  const stages = useStages();
  const members = useMembers();
  const canDelete = useHasRole("manager");
  const invalidate = useInvalidate();
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [tagOpen, setTagOpen] = useState(false);
  const [tag, setTag] = useState("");

  const bulk = useMutation({
    mutationFn: (body: Omit<Schemas["BulkLeadRequest"], "lead_ids">) =>
      unwrap(api.POST("/api/v1/leads/bulk", { body: { ...body, lead_ids: ids } })),
    onSuccess: (r, vars) => {
      const verbs: Record<Schemas["BulkLeadAction"], string> = { assign: "Reassigned", move_stage: "Moved", add_tags: "Tagged", delete: "Archived" };
      const verb = verbs[vars.action];
      toast.success(`${verb} ${r.affected} ${r.affected === 1 ? "lead" : "leads"}`);
      setConfirmArchive(false);
      setTagOpen(false);
      setTag("");
      onDone();
      void invalidate();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const count = `${ids.length} ${ids.length === 1 ? "lead" : "leads"}`;
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line bg-accent-soft/50 px-4 py-2" role="toolbar" aria-label="Bulk actions">
      <span className="mr-1 text-[13px] font-medium text-accent-soft-ink">{count} selected</span>
      <MenuRoot>
        <MenuTrigger asChild>
          <Button size="sm">
            Assign <ChevronDown />
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          <MenuLabel>Assign to</MenuLabel>
          {members.data?.map((m) => (
            <MenuItem key={m.user.id} onSelect={() => bulk.mutate({ action: "assign", owner_user_id: m.user.id })}>
              {m.user.full_name}
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuItem onSelect={() => bulk.mutate({ action: "assign", owner_user_id: null })}>Remove owner</MenuItem>
        </MenuContent>
      </MenuRoot>
      <MenuRoot>
        <MenuTrigger asChild>
          <Button size="sm">
            Move to stage <ChevronDown />
          </Button>
        </MenuTrigger>
        <MenuContent align="start">
          {stages.data?.map((s) => (
            <MenuItem key={s.id} onSelect={() => bulk.mutate({ action: "move_stage", stage_id: s.id })}>
              {s.name}
            </MenuItem>
          ))}
        </MenuContent>
      </MenuRoot>
      <Button size="sm" onClick={() => setTagOpen(true)}>
        Add tag
      </Button>
      {canDelete ? (
        <Button size="sm" variant="ghost" className="text-danger hover:text-danger" onClick={() => setConfirmArchive(true)}>
          Archive
        </Button>
      ) : null}
      <Button size="sm" variant="ghost" className="ml-auto" onClick={onDone}>
        Clear selection
      </Button>

      <ConfirmDialog
        open={confirmArchive}
        onOpenChange={setConfirmArchive}
        title={`Archive ${count}?`}
        description="Archived leads disappear from lists, search and campaigns. Their call history and audit records are kept."
        confirmLabel={`Archive ${count}`}
        loading={bulk.isPending}
        onConfirm={() => bulk.mutate({ action: "delete" })}
      />
      <Dialog
        open={tagOpen}
        onOpenChange={setTagOpen}
        title={`Tag ${count}`}
        width="max-w-sm"
        footer={
          <>
            <Button onClick={() => setTagOpen(false)}>Cancel</Button>
            <Button variant="primary" disabled={!tag.trim()} loading={bulk.isPending} onClick={() => bulk.mutate({ action: "add_tags", tags: [tag.trim()] })}>
              Add tag
            </Button>
          </>
        }
      >
        <Field label="Tag" hint="Lowercase letters, numbers, dashes. Existing tags are kept.">
          <Input value={tag} autoFocus onChange={(e) => setTag(e.target.value.toLowerCase())} placeholder="q4-campaign" />
        </Field>
      </Dialog>
    </div>
  );
}
