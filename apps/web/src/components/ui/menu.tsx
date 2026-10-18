import { Check } from "lucide-react";
import { DropdownMenu as Menu } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export const MenuRoot = Menu.Root;
export const MenuTrigger = Menu.Trigger;
export const MenuLabel = ({ children }: { children: ReactNode }) => (
  <Menu.Label className="px-2 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">{children}</Menu.Label>
);
export const MenuSeparator = () => <Menu.Separator className="my-1 h-px bg-line" />;

export function MenuContent({
  children,
  align = "end",
  className,
}: {
  children: ReactNode;
  align?: "start" | "end" | "center";
  className?: string;
}) {
  return (
    <Menu.Portal>
      <Menu.Content
        align={align}
        sideOffset={6}
        className={cn("z-50 min-w-48 rounded-lg border border-line bg-surface p-1 shadow-pop", className)}
      >
        {children}
      </Menu.Content>
    </Menu.Portal>
  );
}

const itemClass =
  "flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-ink-2 outline-none " +
  "data-[highlighted]:bg-surface-2 data-[highlighted]:text-ink data-[disabled]:opacity-50 [&_svg]:size-4 [&_svg]:text-muted";

/** Toggle that keeps the menu open, for show/hide lists such as grid columns. */
export function MenuCheckboxItem({
  children,
  checked,
  onCheckedChange,
  disabled,
}: {
  children: ReactNode;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <Menu.CheckboxItem
      checked={checked}
      disabled={disabled}
      onCheckedChange={(v) => onCheckedChange(v === true)}
      onSelect={(e) => e.preventDefault()}
      className={cn(itemClass, "relative pl-7")}
    >
      <Menu.ItemIndicator className="absolute left-2">
        <Check className="!text-accent" />
      </Menu.ItemIndicator>
      {children}
    </Menu.CheckboxItem>
  );
}

export function MenuItem({
  children,
  onSelect,
  tone,
  disabled,
}: {
  children: ReactNode;
  onSelect?: () => void;
  tone?: "danger";
  disabled?: boolean;
}) {
  return (
    <Menu.Item
      disabled={disabled}
      onSelect={onSelect}
      className={cn(itemClass, tone === "danger" && "text-danger data-[highlighted]:text-danger [&_svg]:text-danger")}
    >
      {children}
    </Menu.Item>
  );
}
