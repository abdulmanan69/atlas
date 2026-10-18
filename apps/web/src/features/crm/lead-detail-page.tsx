import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import {
  ArrowLeft,
  Archive,
  ArrowRightLeft,
  Bot,
  CheckSquare,
  Copy,
  FileUp,
  MoreHorizontal,
  Pencil,
  Phone as PhoneIcon,
  Plus,
  StickyNote,
  Cog,
  type LucideIcon,
} from "lucide-react";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState, Phone } from "@/components/common";
import { StageTrack } from "@/components/stage-track";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { DateTimePicker } from "@/components/ui/date-time-picker";
import { ConfirmDialog } from "@/components/ui/dialog";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Avatar, Badge, Card, CardHeader, Checkbox, Skeleton, Textarea } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { LeadFormDialog } from "@/features/crm/lead-form-dialog";
import { qk, useInvalidate, useLead, useMemberNames, useMembers, useOrgId, useStages, useTasks, type Activity, type Lead } from "@/features/queries";
import { TaskDialog } from "@/features/tasks/task-dialog";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { formatDateTime, formatRelative, fullName } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

type LeadPatch = Schemas["LeadUpdate"];

export default function LeadDetailPage() {
  const { leadId = "" } = useParams();
  const lead = useLead(leadId);

  if (lead.isError) {
    return (
      <Card>
        <ErrorState error={lead.error} onRetry={() => void lead.refetch()} />
      </Card>
    );
  }
  if (!lead.data) {
    return (
      <div className="flex flex-col gap-5">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-24" />
        <div className="grid gap-5 lg:grid-cols-3">
          <Skeleton className="h-80 lg:col-span-2" />
          <Skeleton className="h-80" />
        </div>
      </div>
    );
  }
  return <LeadView lead={lead.data} />;
}

function useUpdateLead(lead: Lead) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: LeadPatch) => unwrap(api.PATCH("/api/v1/leads/{lead_id}", { params: { path: { lead_id: lead.id } }, body })),
    onSuccess: () => void invalidate(),
    onError: (e) => toast.error(errorMessage(e)),
  });
}

function LeadView({ lead }: { lead: Lead }) {
  const canWrite = useHasRole("agent");
  const canDelete = useHasRole("manager");
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const stages = useStages();
  const update = useUpdateLead(lead);

  const archive = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/leads/{lead_id}", { params: { path: { lead_id: lead.id } } })),
    onSuccess: () => {
      toast.success("Lead archived");
      void invalidate();
      void navigate("/leads");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const name = fullName(lead.first_name, lead.last_name);
  const subtitle = [lead.job_title, lead.company].filter(Boolean).join(" · ");

  return (
    <>
      <Link to="/leads" className="mb-4 inline-flex items-center gap-1 text-[12.5px] font-medium text-muted hover:text-ink">
        <ArrowLeft className="size-3.5" /> Leads
      </Link>
      <header className="flex flex-wrap items-start justify-between gap-4 pb-5">
        <div className="min-w-0">
          <h1 className="title-display text-[28px]">{name}</h1>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[13.5px] text-muted">
            {subtitle ? <span>{subtitle}</span> : null}
            {lead.do_not_call ? <Badge tone="danger">Do not call</Badge> : null}
            {lead.qualification_status === "qualified" ? <Badge tone="success">Qualified</Badge> : null}
            {lead.qualification_status === "unqualified" ? <Badge>Not qualified</Badge> : null}
          </div>
        </div>
        {canWrite ? (
          <div className="flex gap-2">
            <Button onClick={() => setEditing(true)}>
              <Pencil /> Edit details
            </Button>
            {canDelete ? (
              <MenuRoot>
                <MenuTrigger asChild>
                  <Button size="icon" aria-label="More actions">
                    <MoreHorizontal />
                  </Button>
                </MenuTrigger>
                <MenuContent>
                  <MenuItem tone="danger" onSelect={() => setArchiving(true)}>
                    <Archive /> Archive lead
                  </MenuItem>
                </MenuContent>
              </MenuRoot>
            ) : null}
          </div>
        ) : null}
      </header>

      <Card className="mb-5 px-4 py-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          {stages.data ? <StageTrack stages={stages.data} currentStageId={lead.stage_id} size="md" /> : <Skeleton className="h-4 w-40" />}
          <span className="text-[12px] text-muted">Last contacted {lead.last_contacted_at ? formatRelative(lead.last_contacted_at) : "never"}</span>
        </div>
        <div className="mt-4 flex flex-wrap gap-1.5" role="group" aria-label="Move to stage">
          {stages.data?.map((s) => {
            const current = s.id === lead.stage_id;
            return (
              <button
                key={s.id}
                disabled={!canWrite || current || update.isPending}
                aria-pressed={current}
                onClick={() =>
                  update.mutate({ stage_id: s.id }, { onSuccess: () => toast.success(`Moved to ${s.name}`) })
                }
                className={cn(
                  "h-7 rounded-md border px-2.5 text-[12.5px] font-medium transition-colors",
                  current
                    ? s.category === "won"
                      ? "border-success bg-success-soft text-success"
                      : s.category === "lost"
                        ? "border-line-strong bg-surface-2 text-muted"
                        : "border-accent bg-accent-soft text-accent-soft-ink"
                    : "border-line bg-surface text-ink-2 hover:border-line-strong hover:text-ink disabled:cursor-default disabled:hover:border-line",
                )}
              >
                {s.name}
              </button>
            );
          })}
        </div>
      </Card>

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-2">
          <Timeline lead={lead} />
        </div>
        <div className="flex flex-col gap-5">
          <DetailsCard lead={lead} />
          <LeadTasks lead={lead} />
        </div>
      </div>

      <LeadFormDialog open={editing} onOpenChange={setEditing} lead={lead} />
      <ConfirmDialog
        open={archiving}
        onOpenChange={setArchiving}
        title={`Archive ${name}?`}
        description="The lead disappears from lists, search and campaigns. Its call history and audit records are kept."
        confirmLabel="Archive lead"
        loading={archive.isPending}
        onConfirm={() => archive.mutate()}
      />
    </>
  );
}

const ACTIVITY_ICON: Record<Activity["type"], LucideIcon> = {
  note: StickyNote,
  call: PhoneIcon,
  stage_change: ArrowRightLeft,
  task: CheckSquare,
  import: FileUp,
  system: Cog,
  update: Pencil,
};

function Timeline({ lead }: { lead: Lead }) {
  const orgId = useOrgId();
  const names = useMemberNames();
  const canWrite = useHasRole("agent");
  const invalidate = useInvalidate();
  const [note, setNote] = useState("");

  const timeline = useInfiniteQuery({
    queryKey: qk.timeline(orgId, lead.id),
    initialPageParam: 1,
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/api/v1/leads/{lead_id}/activities", { params: { path: { lead_id: lead.id }, query: { page: pageParam, page_size: 30 } } })),
    getNextPageParam: (last) => (last.page * last.page_size < last.total ? last.page + 1 : undefined),
  });

  const addNote = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/leads/{lead_id}/notes", { params: { path: { lead_id: lead.id } }, body: { body: note.trim() } })),
    onSuccess: () => {
      setNote("");
      void invalidate("lead", lead.id);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const items = timeline.data?.pages.flatMap((p) => p.items) ?? [];
  const actor = (a: Activity) =>
    a.actor_type === "ai_agent" ? "AI agent" : a.actor_type === "system" ? "System" : a.actor_user_id ? (names.get(a.actor_user_id) ?? "Teammate") : "Someone";

  return (
    <Card>
      <CardHeader title="Activity" description="Calls, notes, stage changes and tasks, newest first" />
      {canWrite ? (
        <form
          className="border-b border-line px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (note.trim()) addNote.mutate();
          }}
        >
          <Textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Add a note for your team…"
            aria-label="Note"
            maxLength={10_000}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && note.trim()) addNote.mutate();
            }}
          />
          <div className="mt-2 flex items-center justify-between">
            <span className="text-[11.5px] text-faint">Ctrl + Enter to save</span>
            <Button type="submit" size="sm" variant="primary" disabled={!note.trim()} loading={addNote.isPending}>
              Add note
            </Button>
          </div>
        </form>
      ) : null}
      {timeline.isError ? (
        <ErrorState error={timeline.error} onRetry={() => void timeline.refetch()} />
      ) : !timeline.data ? (
        <div className="flex flex-col gap-3 p-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-10" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState title="No activity yet" />
      ) : (
        <ol className="relative px-4 py-4">
          <span aria-hidden className="absolute bottom-6 left-[29px] top-6 w-px bg-line" />
          {items.map((a) => {
            const Icon = a.actor_type === "ai_agent" ? Bot : ACTIVITY_ICON[a.type];
            return (
              <li key={a.id} className="relative flex gap-3 pb-4 last:pb-0">
                <span
                  className={cn(
                    "relative z-[1] grid size-7 shrink-0 place-items-center rounded-full border border-line bg-surface text-muted",
                    a.type === "stage_change" && "border-accent/40 text-accent",
                    a.type === "call" && "border-success/40 text-success",
                  )}
                >
                  <Icon className="size-3.5" aria-hidden />
                </span>
                <div className="min-w-0 flex-1 pt-0.5">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <p className="text-[13px] font-medium text-ink">{a.type === "note" ? "Note" : a.summary}</p>
                    <time dateTime={a.occurred_at} title={formatDateTime(a.occurred_at)} className="text-[12px] text-faint">
                      {formatRelative(a.occurred_at)}
                    </time>
                  </div>
                  <p className="text-[12px] text-muted">{actor(a)}</p>
                  {a.body ? <p className="mt-1.5 whitespace-pre-wrap rounded-md bg-surface-2 px-3 py-2 text-[13px] text-ink-2">{a.body}</p> : null}
                </div>
              </li>
            );
          })}
          {timeline.hasNextPage ? (
            <li className="relative pl-10 pt-2">
              <Button size="sm" variant="ghost" loading={timeline.isFetchingNextPage} onClick={() => void timeline.fetchNextPage()}>
                Show older activity
              </Button>
            </li>
          ) : null}
        </ol>
      )}
    </Card>
  );
}

/** Saves when the picker closes (or is cleared), not on every keystroke in the time fields. */
function FollowUpField({ lead, onSave }: { lead: Lead; onSave: (iso: string | null) => void }) {
  const [draft, setDraft] = useState<string | null>(lead.next_follow_up_at);
  const latest = useRef(draft);
  const commit = (v: string | null) => {
    if (v !== (lead.next_follow_up_at ?? null)) onSave(v);
  };
  return (
    <DateTimePicker
      aria-label="Next follow-up"
      size="sm"
      clearable
      value={draft}
      onChange={(v) => {
        latest.current = v;
        setDraft(v);
        if (v === null) commit(null);
      }}
      onBlur={() => commit(latest.current)}
    />
  );
}

function DetailsCard({ lead }: { lead: Lead }) {
  const canWrite = useHasRole("agent");
  const members = useMembers();
  const names = useMemberNames();
  const update = useUpdateLead(lead);
  const saved = () => toast.success("Saved");

  const row = (label: string, value: React.ReactNode) => (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-3 px-4 py-2.5">
      <dt className="text-[12.5px] text-muted">{label}</dt>
      <dd className="min-w-0 text-[13px]">{value}</dd>
    </div>
  );

  return (
    <Card>
      <CardHeader title="Details" />
      <dl className="divide-y divide-line">
        {row(
          "Phone",
          lead.phone_e164 ? (
            <span className="flex items-center gap-1">
              <Phone value={lead.phone_display} />
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label="Copy phone number"
                onClick={() => void navigator.clipboard.writeText(lead.phone_e164 ?? "").then(() => toast.success("Phone number copied"))}
              >
                <Copy className="!size-3.5" />
              </Button>
            </span>
          ) : (
            <span className="text-faint">—</span>
          ),
        )}
        {row(
          "Email",
          lead.email ? (
            <a href={`mailto:${lead.email}`} className="truncate text-accent hover:underline">
              {lead.email}
            </a>
          ) : (
            <span className="text-faint">—</span>
          ),
        )}
        {row(
          "Owner",
          canWrite && members.data ? (
            <Combobox
              aria-label="Owner"
              size="sm"
              value={lead.owner_user_id}
              onValueChange={(v) => update.mutate({ owner_user_id: v }, { onSuccess: saved })}
              placeholder="Unassigned"
              searchPlaceholder="Search teammates"
              allowClear
              options={members.data.map((m) => ({
                value: m.user.id,
                label: m.user.full_name,
                description: m.user.email,
                icon: <Avatar name={m.user.full_name} className="size-4 text-[8px]" />,
              }))}
            />
          ) : (
            (lead.owner_user_id && names.get(lead.owner_user_id)) || "Unassigned"
          ),
        )}
        {row(
          "Consent",
          canWrite ? (
            <Select
              aria-label="Consent"
              size="sm"
              value={lead.consent_status}
              onValueChange={(v) => update.mutate({ consent_status: v as Lead["consent_status"] }, { onSuccess: saved })}
            >
              <SelectItem value="unknown">Not recorded</SelectItem>
              <SelectItem value="granted">Granted</SelectItem>
              <SelectItem value="revoked" description="Implies do-not-call">
                Revoked
              </SelectItem>
            </Select>
          ) : (
            lead.consent_status
          ),
        )}
        {row(
          "Do not call",
          <label className="flex items-center gap-2">
            <Switch
              aria-label="Do not call"
              size="sm"
              checked={lead.do_not_call}
              disabled={!canWrite}
              onCheckedChange={(v) => update.mutate({ do_not_call: v }, { onSuccess: saved })}
            />
            <span className="text-[12.5px] text-muted">{lead.do_not_call ? "Never call this lead" : "Can be called"}</span>
          </label>,
        )}
        {row(
          "Follow-up",
          canWrite ? (
            <FollowUpField
              key={lead.next_follow_up_at ?? "none"}
              lead={lead}
              onSave={(v) => update.mutate({ next_follow_up_at: v }, { onSuccess: saved })}
            />
          ) : (
            formatDateTime(lead.next_follow_up_at)
          ),
        )}
        {row("Source", <span className="font-mono text-[12.5px]">{lead.source ?? "—"}</span>)}
        {row(
          "Tags",
          lead.tags.length ? (
            <span className="flex flex-wrap gap-1">
              {lead.tags.map((t) => (
                <Link key={t} to={`/leads?tag=${encodeURIComponent(t)}`}>
                  <Badge>{t}</Badge>
                </Link>
              ))}
            </span>
          ) : (
            <span className="text-faint">—</span>
          ),
        )}
        {row("Added", <span title={formatDateTime(lead.created_at)}>{formatRelative(lead.created_at)}</span>)}
      </dl>
    </Card>
  );
}

function LeadTasks({ lead }: { lead: Lead }) {
  const tasks = useTasks({ lead_id: lead.id, status: "open", page_size: 20 });
  const canWrite = useHasRole("agent");
  const invalidate = useInvalidate();
  const [adding, setAdding] = useState(false);
  const complete = useMutation({
    mutationFn: (id: string) => unwrap(api.PATCH("/api/v1/tasks/{task_id}", { params: { path: { task_id: id } }, body: { status: "done" } })),
    onSuccess: () => {
      toast.success("Task completed");
      void invalidate();
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Card>
      <CardHeader
        title="Open tasks"
        action={
          canWrite ? (
            <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>
              <Plus /> Add task
            </Button>
          ) : null
        }
      />
      {tasks.data && tasks.data.items.length === 0 ? (
        <p className="px-4 py-5 text-[13px] text-muted">No open tasks for this lead.</p>
      ) : (
        <ul className="divide-y divide-line">
          {(tasks.data?.items ?? []).map((t) => {
            const overdue = t.due_at ? new Date(t.due_at).getTime() < Date.now() : false;
            return (
              <li key={t.id} className="flex items-start gap-2.5 px-4 py-2.5">
                <Checkbox
                  aria-label={`Complete ${t.title}`}
                  checked={false}
                  onCheckedChange={() => canWrite && complete.mutate(t.id)}
                  className="mt-0.5"
                />
                <div className="min-w-0">
                  <p className="text-[13px] font-medium">{t.title}</p>
                  <p className={cn("text-[12px]", overdue ? "text-danger" : "text-muted")}>
                    {t.due_at ? `Due ${formatRelative(t.due_at)}` : "No due date"}
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <TaskDialog open={adding} onOpenChange={setAdding} leadId={lead.id} />
    </Card>
  );
}
