import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Copies `value` and confirms inline for two seconds. Shared by secrets, keys and webhook URLs. */
function useCopy(): [copied: boolean, copy: (value: string, toastText?: string) => void] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  const copy = (value: string, toastText?: string) => {
    void navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true);
        if (toastText) toast.success(toastText);
        if (timer.current) clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 2000);
      })
      .catch(() => toast.error("Couldn't copy. Select the text and copy it yourself."));
  };
  return [copied, copy];
}

export function CopyButton({
  value,
  label,
  size = "icon-sm",
  variant = "ghost",
  children,
  className,
}: {
  value: string;
  /** Accessible name, e.g. "Copy inbound webhook URL". */
  label: string;
  size?: "sm" | "md" | "icon-sm" | "icon";
  variant?: "ghost" | "secondary" | "primary";
  children?: ReactNode;
  className?: string;
}) {
  const [copied, copy] = useCopy();
  return (
    <Button
      size={size}
      variant={variant}
      aria-label={copied ? "Copied" : label}
      title={copied ? "Copied" : label}
      onClick={() => copy(value)}
      className={cn(copied && "text-success", className)}
    >
      {copied ? <Check /> : <Copy />}
      {children}
    </Button>
  );
}

/** Read-only mono value with a copy control; used for URLs, keys and secrets. */
export function CopyField({ value, label, className }: { value: string; label: string; className?: string }) {
  return (
    <div className={cn("flex items-center gap-1 rounded-md border border-line-strong bg-surface-2/60 pl-2.5 pr-1", className)}>
      <code className="min-w-0 flex-1 select-all truncate py-1.5 font-mono text-[12px] text-ink" title={value} aria-label={label}>
        {value}
      </code>
      <CopyButton value={value} label={`Copy ${label.toLowerCase()}`} />
    </div>
  );
}
