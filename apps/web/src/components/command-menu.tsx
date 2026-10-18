import { useQuery } from "@tanstack/react-query";
import { Command } from "cmdk";
import { Dialog as DialogPrimitive } from "radix-ui";
import { useDeferredValue, useState } from "react";
import { useNavigate } from "react-router";

import { NAV } from "@/components/nav";
import { api, unwrap } from "@/lib/api";
import { fullName } from "@/lib/format";
import { useSession } from "@/lib/session";

export function CommandMenu({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const orgId = useSession((s) => s.orgId);
  const [search, setSearch] = useState("");
  const q = useDeferredValue(search.trim());

  const leads = useQuery({
    queryKey: ["org", orgId, "leads", "command", q],
    queryFn: () => unwrap(api.GET("/api/v1/leads", { params: { query: { q, page_size: 6 } } })),
    enabled: open && q.length >= 2,
  });

  const go = (to: string) => {
    onOpenChange(false);
    setSearch("");
    void navigate(to);
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-ink/40" />
        <DialogPrimitive.Content className="fixed left-1/2 top-[14vh] z-50 w-[calc(100vw-2rem)] max-w-xl -translate-x-1/2 overflow-hidden rounded-xl border border-line bg-surface shadow-pop">
          <DialogPrimitive.Title className="sr-only">Search</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Search leads or jump to a page</DialogPrimitive.Description>
          <Command shouldFilter={false} label="Search">
            <Command.Input
              value={search}
              onValueChange={setSearch}
              placeholder="Search leads by name, company, email or phone…"
              className="h-12 w-full border-b border-line bg-transparent px-4 text-[14px] outline-none placeholder:text-faint"
            />
            <Command.List className="max-h-80 overflow-y-auto p-1.5">
              {q.length >= 2 && leads.data?.items.length === 0 ? (
                <div className="px-3 py-6 text-center text-[13px] text-muted">No leads match “{q}”.</div>
              ) : null}
              {leads.data?.items.length ? (
                <Command.Group heading="Leads" className="cmdk-group">
                  {leads.data.items.map((l) => (
                    <Command.Item key={l.id} value={l.id} onSelect={() => go(`/leads/${l.id}`)} className="cmdk-item">
                      <span className="font-medium text-ink">{fullName(l.first_name, l.last_name)}</span>
                      {l.company ? <span className="text-muted">· {l.company}</span> : null}
                    </Command.Item>
                  ))}
                </Command.Group>
              ) : null}
              <Command.Group heading="Go to" className="cmdk-group">
                {NAV.filter((n) => n.label.toLowerCase().includes(q.toLowerCase())).map((n) => (
                  <Command.Item key={n.to} value={n.to} onSelect={() => go(n.to)} className="cmdk-item">
                    <n.icon className="size-4 text-muted" />
                    {n.label}
                  </Command.Item>
                ))}
              </Command.Group>
            </Command.List>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
