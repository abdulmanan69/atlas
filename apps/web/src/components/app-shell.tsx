import { useMutation } from "@tanstack/react-query";
import {
  Check,
  ChevronsUpDown,
  LogOut,
  Menu,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Search,
  Settings,
  Sun,
} from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";
import { toast } from "sonner";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { Mark, PRODUCT_NAME, Wordmark } from "@/components/brand";
import { CommandMenu } from "@/components/command-menu";
import { Logo } from "@/components/common";
import { NAV, type NavItem } from "@/components/nav";
import { Button } from "@/components/ui/button";
import { FormCombobox } from "@/components/ui/combobox";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuLabel, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Avatar, Input, Kbd } from "@/components/ui/primitives";
import { api, CSRF_HEADERS, errorMessage, unwrap } from "@/lib/api";
import { session, useCurrentMembership, useSession, type Role } from "@/lib/session";
import { useTheme, type ThemePref } from "@/lib/theme";
import { useCommandMenu } from "@/lib/hooks";
import { cn, TIMEZONE_OPTIONS } from "@/lib/utils";

const RANK: Record<Role, number> = { viewer: 0, agent: 1, manager: 2, admin: 3, owner: 4 };

function useSidebarCollapsed(): [boolean, (v: boolean) => void] {
  const [v, setV] = useState(() => {
    try {
      return localStorage.getItem("atlas-sidebar") === "collapsed";
    } catch {
      return false;
    }
  });
  return [
    v,
    (next) => {
      setV(next);
      try {
        localStorage.setItem("atlas-sidebar", next ? "collapsed" : "expanded");
      } catch {
        /* not persisted */
      }
    },
  ];
}

export function AppShell() {
  const [collapsed, setCollapsed] = useSidebarCollapsed();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [cmdOpen, setCmdOpen] = useCommandMenu();
  const location = useLocation();

  return (
    <div className="flex min-h-dvh">
      <aside
        className={cn(
          "sticky top-0 hidden h-dvh shrink-0 flex-col border-r border-line bg-surface transition-[width] duration-150 md:flex",
          collapsed ? "w-[60px]" : "w-[232px]",
        )}
      >
        <Sidebar collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />
      </aside>

      <DialogPrimitive.Root open={mobileOpen} onOpenChange={setMobileOpen}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-ink/40 md:hidden" />
          <DialogPrimitive.Content
            className="fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-line bg-surface md:hidden"
            onClick={(e) => {
              if ((e.target as HTMLElement).closest("a")) setMobileOpen(false);
            }}
          >
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <DialogPrimitive.Description className="sr-only">Main navigation</DialogPrimitive.Description>
            <Sidebar collapsed={false} />
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onMenu={() => setMobileOpen(true)} onSearch={() => setCmdOpen(true)} />
        <main key={location.pathname.split("/")[1]} className="mx-auto w-full max-w-[1440px] flex-1 px-4 py-6 md:px-8 md:py-8">
          <Outlet />
        </main>
      </div>
      <CommandMenu open={cmdOpen} onOpenChange={setCmdOpen} />
    </div>
  );
}

function Sidebar({ collapsed, onToggle }: { collapsed: boolean; onToggle?: () => void }) {
  const role = useCurrentMembership()?.role ?? "viewer";
  const visible = (n: NavItem) => !n.minRole || RANK[role] >= RANK[n.minRole];
  const groups = ["Pipeline", "Voice AI", "Organization"] as const;

  return (
    <>
      <div className={cn("flex h-12 items-center border-b border-line", collapsed ? "justify-center" : "px-4")}>
        <NavLink to="/" className="flex items-center rounded-md text-ink" aria-label={`${PRODUCT_NAME} home`}>
          {collapsed ? <Mark className="h-[18px]" title={null} /> : <Wordmark className="h-[14px]" title={null} />}
        </NavLink>
      </div>
      <div className={cn("flex h-12 items-center border-b border-line", collapsed ? "justify-center px-2" : "px-3")}>
        <OrgSwitcher collapsed={collapsed} />
      </div>
      <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label="Main">
        {groups.map((g) => (
          <div key={g} className="mb-4">
            {!collapsed ? <div className="eyebrow px-2 pb-1.5 text-[10.5px] text-faint">{g}</div> : null}
            <ul className="flex flex-col gap-px">
              {NAV.filter((n) => n.group === g && visible(n)).map((n) => (
                <li key={n.to}>
                  <NavLink
                    to={n.to}
                    end={n.to === "/"}
                    title={collapsed ? n.label : undefined}
                    className={({ isActive }) =>
                      cn(
                        "relative flex h-8 items-center gap-2.5 rounded-md text-[13px] font-medium text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink",
                        collapsed ? "justify-center px-0" : "px-2",
                        isActive &&
                          "bg-surface-2 text-ink before:absolute before:-left-2 before:top-1.5 before:h-5 before:w-[3px] before:rounded-r before:bg-accent",
                      )
                    }
                  >
                    <n.icon className="size-4 shrink-0 text-muted" aria-hidden />
                    {!collapsed ? <span className="truncate">{n.label}</span> : <span className="sr-only">{n.label}</span>}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>
      <div className={cn("border-t border-line p-2", collapsed && "flex flex-col items-center gap-1")}>
        <UserMenu collapsed={collapsed} />
        {onToggle ? (
          <Button
            variant="ghost"
            size={collapsed ? "icon" : "sm"}
            className={cn("mt-1 text-muted", !collapsed && "w-full justify-start")}
            onClick={onToggle}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
            {!collapsed ? "Collapse" : null}
          </Button>
        ) : null}
      </div>
    </>
  );
}

function OrgSwitcher({ collapsed }: { collapsed: boolean }) {
  const memberships = useSession((s) => s.memberships);
  const current = useCurrentMembership();
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const name = current?.organization.name ?? "No organization";

  return (
    <>
      <MenuRoot>
        <MenuTrigger asChild>
          <button
            className={cn(
              "flex min-w-0 items-center gap-2 rounded-md text-left transition-colors hover:bg-surface-2",
              collapsed ? "p-1" : "-mx-1 w-[calc(100%+0.5rem)] px-1.5 py-1",
            )}
            aria-label={`Organization: ${name}. Switch organization`}
          >
            <span className="grid size-7 shrink-0 place-items-center rounded-md bg-ink text-[12px] font-semibold text-canvas">
              {name.slice(0, 1).toUpperCase()}
            </span>
            {!collapsed ? (
              <>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-semibold leading-tight">{name}</span>
                  <span className="block text-[11px] capitalize leading-tight text-muted">{current?.role}</span>
                </span>
                <ChevronsUpDown className="size-3.5 shrink-0 text-faint" aria-hidden />
              </>
            ) : null}
          </button>
        </MenuTrigger>
        <MenuContent align="start" className="w-64">
          <MenuLabel>Organizations</MenuLabel>
          {memberships.map((m) => (
            <MenuItem
              key={m.id}
              onSelect={() => {
                session.getState().selectOrg(m.organization.id);
                void navigate("/");
              }}
            >
              <span className="min-w-0 flex-1 truncate">{m.organization.name}</span>
              {m.organization.id === current?.organization.id ? <Check className="!text-accent" /> : null}
            </MenuItem>
          ))}
          <MenuSeparator />
          <MenuItem onSelect={() => setCreating(true)}>
            <Plus /> New organization
          </MenuItem>
        </MenuContent>
      </MenuRoot>
      <CreateOrgDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}

const orgSchema = z.object({
  name: z.string().trim().min(2, "Use at least 2 characters").max(200),
  timezone: z.string().min(1),
  default_country: z.string().regex(/^[A-Z]{2}$/, "Two-letter country code, e.g. GB"),
});

function CreateOrgDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate();
  const form = useForm<z.infer<typeof orgSchema>>({
    resolver: zodResolver(orgSchema),
    defaultValues: { name: "", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC", default_country: "US" },
  });
  const create = useMutation({
    mutationFn: (body: z.infer<typeof orgSchema>) => unwrap(api.POST("/api/v1/organizations", { body })),
    onSuccess: async (membership) => {
      const me = await unwrap(api.GET("/api/v1/auth/me"));
      session.getState().setMemberships(me.memberships);
      session.getState().selectOrg(membership.organization.id);
      toast.success(`Created ${membership.organization.name}`);
      onOpenChange(false);
      form.reset();
      void navigate("/");
    },
  });
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New organization"
      description="You'll be its owner. Leads, agents and members are kept separate per organization."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" loading={create.isPending} onClick={form.handleSubmit((v) => create.mutate(v))}>
            Create organization
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-4" onSubmit={form.handleSubmit((v) => create.mutate(v))}>
        <FormError message={create.error ? errorMessage(create.error) : null} />
        <Field label="Name" required error={form.formState.errors.name?.message}>
          <Input autoFocus {...form.register("name")} />
        </Field>
        <div className="grid grid-cols-[1fr_120px] gap-3">
          <Field label="Time zone" hint="Used for calling hours and daily reports">
            <FormCombobox control={form.control} name="timezone" options={TIMEZONE_OPTIONS} searchPlaceholder="Search time zones" />
          </Field>
          <Field label="Country" hint="Default for phone numbers" error={form.formState.errors.default_country?.message}>
            <Input maxLength={2} className="uppercase" {...form.register("default_country", { setValueAs: (v: string) => v.toUpperCase() })} />
          </Field>
        </div>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

function UserMenu({ collapsed }: { collapsed: boolean }) {
  const user = useSession((s) => s.user);
  const navigate = useNavigate();
  const signOut = async () => {
    try {
      await api.POST("/api/v1/auth/logout", { headers: CSRF_HEADERS });
    } finally {
      session.getState().signOut();
      void navigate("/sign-in");
    }
  };
  if (!user) return null;
  return (
    <MenuRoot>
      <MenuTrigger asChild>
        <button
          className={cn(
            "flex w-full min-w-0 items-center gap-2 rounded-md p-1.5 text-left transition-colors hover:bg-surface-2",
            collapsed && "w-auto",
          )}
          aria-label={`Account: ${user.full_name}`}
        >
          <Avatar name={user.full_name} />
          {!collapsed ? (
            <span className="min-w-0">
              <span className="block truncate text-[13px] font-medium leading-tight">{user.full_name}</span>
              <span className="block truncate text-[11.5px] leading-tight text-muted">{user.email}</span>
            </span>
          ) : null}
        </button>
      </MenuTrigger>
      <MenuContent align="start" className="w-56">
        <MenuItem onSelect={() => void navigate("/settings")}>
          <Settings /> Settings
        </MenuItem>
        <MenuSeparator />
        <MenuItem onSelect={() => void signOut()}>
          <LogOut /> Sign out
        </MenuItem>
      </MenuContent>
    </MenuRoot>
  );
}

function TopBar({ onMenu, onSearch }: { onMenu: () => void; onSearch: () => void }) {
  const location = useLocation();
  const org = useCurrentMembership()?.organization.name;
  const section = NAV.find((n) => (n.to === "/" ? location.pathname === "/" : location.pathname.startsWith(n.to)));
  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-canvas/90 px-4 backdrop-blur-sm md:px-8">
      <Button variant="ghost" size="icon" className="md:hidden" onClick={onMenu} aria-label="Open navigation">
        <Menu />
      </Button>
      <nav aria-label="Breadcrumb" className="hidden min-w-0 items-center gap-1.5 text-[13px] sm:flex">
        <span className="truncate text-muted">{org}</span>
        <span className="text-faint">/</span>
        <span className="truncate font-medium text-ink">{section?.label ?? "Lead"}</span>
      </nav>
      <div className="md:hidden">
        <Logo />
      </div>
      <div className="ml-auto flex items-center gap-1.5">
        <button
          onClick={onSearch}
          className="flex h-8 items-center gap-2 rounded-md border border-line-strong bg-surface pl-2.5 pr-1.5 text-[13px] text-faint shadow-card transition-colors hover:border-faint sm:w-64"
        >
          <Search className="size-4" aria-hidden />
          <span className="hidden flex-1 text-left sm:inline">Search leads…</span>
          <span className="sr-only sm:hidden">Search</span>
          <span className="hidden gap-0.5 sm:flex">
            <Kbd>Ctrl</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>
        <ThemeMenu />
      </div>
    </header>
  );
}

function ThemeMenu() {
  const [pref, setPref] = useTheme();
  const options: { value: ThemePref; label: string; icon: typeof Sun }[] = [
    { value: "light", label: "Light", icon: Sun },
    { value: "dark", label: "Dark", icon: Moon },
    { value: "system", label: "Match system", icon: Monitor },
  ];
  const Current = options.find((o) => o.value === pref)?.icon ?? Monitor;
  return (
    <MenuRoot>
      <MenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Theme">
          <Current />
        </Button>
      </MenuTrigger>
      <MenuContent className="w-44">
        {options.map((o) => (
          <MenuItem key={o.value} onSelect={() => setPref(o.value)}>
            <o.icon /> <span className="flex-1">{o.label}</span>
            {pref === o.value ? <Check className="!text-accent" /> : null}
          </MenuItem>
        ))}
      </MenuContent>
    </MenuRoot>
  );
}
