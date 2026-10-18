import { AlertTriangle, Loader2 } from "lucide-react";
import type { ReactNode } from "react";

import { Wordmark } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 pb-5">
      <div className="min-w-0">
        {eyebrow ? <div className="eyebrow mb-1.5">{eyebrow}</div> : null}
        <h1 className="title-display text-[26px] text-ink">{title}</h1>
        {description ? <p className="mt-1 max-w-2xl text-[13.5px] text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center px-6 py-14 text-center", className)}>
      {icon ? <div className="mb-3 grid size-10 place-items-center rounded-lg bg-surface-2 text-muted [&_svg]:size-5">{icon}</div> : null}
      <p className="text-[14px] font-semibold text-ink">{title}</p>
      {description ? <p className="mt-1 max-w-sm text-[13px] text-muted">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  const apiErr = error instanceof ApiError ? error : null;
  const forbidden = apiErr?.status === 403;
  return (
    <div className={cn("flex flex-col items-center justify-center px-6 py-12 text-center", className)} role="alert">
      <div className="mb-3 grid size-10 place-items-center rounded-lg bg-danger-soft text-danger">
        <AlertTriangle className="size-5" />
      </div>
      <p className="text-[14px] font-semibold text-ink">{forbidden ? "You don't have access to this" : "This didn't load"}</p>
      <p className="mt-1 max-w-sm text-[13px] text-muted">
        {forbidden ? "Ask an administrator of this organization for the required role." : (apiErr?.message ?? "An unexpected error occurred.")}
      </p>
      {apiErr?.requestId ? (
        <p className="mt-2 font-mono text-[11px] text-faint">Reference {apiErr.requestId}</p>
      ) : null}
      {onRetry && !forbidden ? (
        <Button className="mt-4" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div className="grid min-h-dvh place-items-center bg-canvas">
      <div className="flex items-center gap-2 text-[13px] text-muted" role="status">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {label}
      </div>
    </div>
  );
}

/** Renders a server-formatted phone number (`phone_display`). */
export function Phone({ value, className }: { value: string | null | undefined; className?: string }) {
  if (!value) return <span className="text-faint">—</span>;
  return <span className={cn("font-mono text-[12.5px] text-ink-2", className)}>{value}</span>;
}

export function Logo({ className }: { className?: string }) {
  return <Wordmark className={cn("h-[17px] text-ink", className)} />;
}
