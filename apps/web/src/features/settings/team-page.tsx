import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Copy, MailPlus, MoreHorizontal } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Avatar, Badge, Card, CardHeader, Input, Skeleton } from "@/components/ui/primitives";
import { FormSelect, Select, SelectItem } from "@/components/ui/select";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { qk, useInvalidate, useMembers, useOrgId, type Member } from "@/features/queries";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { formatDate, formatRelative } from "@/lib/format";
import { useCurrentMembership, useHasRole, useSession, type Role } from "@/lib/session";

const ROLES: { value: Role; label: string; description: string }[] = [
  { value: "owner", label: "Owner", description: "Everything, including organization settings" },
  { value: "admin", label: "Admin", description: "Team, integrations, audit log, live campaigns" },
  { value: "manager", label: "Manager", description: "Agents, campaigns, imports and exports" },
  { value: "agent", label: "Agent", description: "Work leads, notes and tasks" },
  { value: "viewer", label: "Viewer", description: "Read-only access" },
];
const RANK: Record<Role, number> = { viewer: 0, agent: 1, manager: 2, admin: 3, owner: 4 };

export default function TeamPage() {
  const members = useMembers();
  const canManage = useHasRole("admin");
  const [inviting, setInviting] = useState(false);

  return (
    <>
      <PageHeader
        title="Team"
        description="Who can access this organization, and what they can do."
        actions={
          canManage ? (
            <Button variant="primary" onClick={() => setInviting(true)}>
              <MailPlus /> Invite member
            </Button>
          ) : null
        }
      />
      <div className="flex flex-col gap-5">
        <Card className="overflow-hidden">
          {members.isError ? (
            <ErrorState error={members.error} onRetry={() => void members.refetch()} />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Member</Th>
                  <Th>Role</Th>
                  <Th>Status</Th>
                  <Th>Joined</Th>
                  <Th className="w-12" />
                </tr>
              </thead>
              <tbody>
                {members.data
                  ? members.data.map((m) => <MemberRow key={m.id} member={m} canManage={canManage} />)
                  : Array.from({ length: 3 }, (_, i) => (
                      <tr key={i}>
                        <Td colSpan={5}>
                          <Skeleton className="h-6" />
                        </Td>
                      </tr>
                    ))}
              </tbody>
            </Table>
          )}
        </Card>
        {canManage ? <Invitations /> : null}
        <Card>
          <CardHeader title="Roles" description="Each role includes everything below it." />
          <dl className="grid gap-px bg-line sm:grid-cols-5">
            {ROLES.map((r) => (
              <div key={r.value} className="bg-surface px-4 py-3">
                <dt className="text-[13px] font-semibold">{r.label}</dt>
                <dd className="mt-0.5 text-[12px] text-muted">{r.description}</dd>
              </div>
            ))}
          </dl>
        </Card>
      </div>
      <InviteDialog open={inviting} onOpenChange={setInviting} />
    </>
  );
}

function MemberRow({ member, canManage }: { member: Member; canManage: boolean }) {
  const me = useSession((s) => s.user?.id);
  const myRole = useCurrentMembership()?.role ?? "viewer";
  const invalidate = useInvalidate();
  const [removing, setRemoving] = useState(false);
  const isMe = member.user.id === me;
  const manageable = canManage && !isMe && RANK[member.role] <= RANK[myRole];

  const update = useMutation({
    mutationFn: (body: Schemas["MemberUpdate"]) =>
      unwrap(api.PATCH("/api/v1/members/{member_id}", { params: { path: { member_id: member.id } }, body })),
    onSuccess: () => {
      toast.success("Member updated");
      void invalidate("members");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/members/{member_id}", { params: { path: { member_id: member.id } } })),
    onSuccess: () => {
      toast.success(`Removed ${member.user.full_name}`);
      setRemoving(false);
      void invalidate("members");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Tr>
      <Td>
        <div className="flex items-center gap-2.5">
          <Avatar name={member.user.full_name} />
          <div className="min-w-0">
            <p className="truncate font-medium">
              {member.user.full_name} {isMe ? <span className="font-normal text-muted">(you)</span> : null}
            </p>
            <p className="truncate text-[12px] text-muted">{member.user.email}</p>
          </div>
        </div>
      </Td>
      <Td>
        {manageable ? (
          <Select
            aria-label={`Role for ${member.user.full_name}`}
            value={member.role}
            onValueChange={(v) => update.mutate({ role: v as Role })}
            size="sm"
            className="w-32"
          >
            {ROLES.filter((r) => RANK[r.value] <= RANK[myRole]).map((r) => (
              <SelectItem key={r.value} value={r.value}>
                {r.label}
              </SelectItem>
            ))}
          </Select>
        ) : (
          <span className="capitalize">{member.role}</span>
        )}
      </Td>
      <Td>{member.status === "active" ? <Badge tone="success">Active</Badge> : <Badge tone="warn">Suspended</Badge>}</Td>
      <Td className="text-muted">{formatDate(member.created_at)}</Td>
      <Td>
        {manageable ? (
          <MenuRoot>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${member.user.full_name}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              {member.status === "active" ? (
                <MenuItem onSelect={() => update.mutate({ status: "suspended" })}>Suspend access</MenuItem>
              ) : (
                <MenuItem onSelect={() => update.mutate({ status: "active" })}>Restore access</MenuItem>
              )}
              <MenuItem tone="danger" onSelect={() => setRemoving(true)}>
                Remove from organization
              </MenuItem>
            </MenuContent>
          </MenuRoot>
        ) : null}
        <ConfirmDialog
          open={removing}
          onOpenChange={setRemoving}
          title={`Remove ${member.user.full_name}?`}
          description="They lose access immediately. Leads they own stay in place and can be reassigned."
          confirmLabel="Remove member"
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />
      </Td>
    </Tr>
  );
}

function Invitations() {
  const orgId = useOrgId();
  const invalidate = useInvalidate();
  const invitations = useQuery({
    queryKey: qk.invitations(orgId),
    queryFn: () => unwrap(api.GET("/api/v1/invitations")),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/v1/invitations/{invitation_id}", { params: { path: { invitation_id: id } } })),
    onSuccess: () => {
      toast.success("Invitation revoked");
      void invalidate("invitations");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const now = Date.now();
  const pending = invitations.data?.filter((i) => !i.accepted_at && !i.revoked_at && new Date(i.expires_at).getTime() > now) ?? [];

  return (
    <Card>
      <CardHeader title="Pending invitations" description="Invitation links expire after 7 days." />
      {invitations.data && pending.length === 0 ? (
        <EmptyState title="No pending invitations" className="py-8" />
      ) : (
        <ul className="divide-y divide-line">
          {pending.map((i) => (
            <li key={i.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]">
              <span className="min-w-0 truncate">
                <span className="font-medium">{i.email}</span> <span className="capitalize text-muted">· {i.role}</span>
              </span>
              <span className="flex items-center gap-3">
                <span className="text-[12px] text-muted">Expires {formatRelative(i.expires_at)}</span>
                <Button size="sm" variant="ghost" loading={revoke.isPending && revoke.variables === i.id} onClick={() => revoke.mutate(i.id)}>
                  Revoke
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const inviteSchema = z.object({ email: z.email("Enter a valid email address"), role: z.enum(["owner", "admin", "manager", "agent", "viewer"]) });

function InviteDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const myRole = useCurrentMembership()?.role ?? "viewer";
  const invalidate = useInvalidate();
  const [link, setLink] = useState<string | null>(null);
  const form = useForm<z.infer<typeof inviteSchema>>({ resolver: zodResolver(inviteSchema), defaultValues: { email: "", role: "agent" } });
  const invite = useMutation({
    mutationFn: (body: z.infer<typeof inviteSchema>) => unwrap(api.POST("/api/v1/invitations", { body })),
    onSuccess: (inv) => {
      setLink(`${window.location.origin}/invite?token=${encodeURIComponent(inv.token)}`);
      void invalidate("invitations");
    },
  });
  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setLink(null);
      form.reset();
      invite.reset();
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={link ? "Invitation created" : "Invite member"}
      description={link ? "Send this link to the invitee. It's shown only once and expires in 7 days." : undefined}
      footer={
        link ? (
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={() => close(false)}>Cancel</Button>
            <Button variant="primary" loading={invite.isPending} onClick={form.handleSubmit((v) => invite.mutate(v))}>
              Create invitation
            </Button>
          </>
        )
      }
    >
      {link ? (
        <div className="flex gap-2">
          <Input readOnly value={link} className="font-mono text-[12px]" onFocus={(e) => e.target.select()} aria-label="Invitation link" />
          <Button onClick={() => void navigator.clipboard.writeText(link).then(() => toast.success("Link copied"))}>
            <Copy /> Copy
          </Button>
        </div>
      ) : (
        <form className="flex flex-col gap-4" noValidate onSubmit={form.handleSubmit((v) => invite.mutate(v))}>
          <FormError message={invite.error ? errorMessage(invite.error) : null} />
          <Field label="Email" required error={form.formState.errors.email?.message}>
            <Input type="email" autoFocus {...form.register("email")} />
          </Field>
          <Field label="Role" hint={ROLES.find((r) => r.value === form.watch("role"))?.description}>
            <FormSelect control={form.control} name="role">
              {ROLES.filter((r) => RANK[r.value] <= RANK[myRole]).map((r) => (
                <SelectItem key={r.value} value={r.value} description={r.description}>
                  {r.label}
                </SelectItem>
              ))}
            </FormSelect>
          </Field>
          <button type="submit" hidden />
        </form>
      )}
    </Dialog>
  );
}
