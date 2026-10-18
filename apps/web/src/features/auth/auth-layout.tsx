import type { ReactNode } from "react";

import { Logo } from "@/components/common";

export function AuthLayout({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
      <div className="flex flex-col px-6 py-8 sm:px-12">
        <Logo />
        <div className="flex flex-1 items-center">
          <div className="mx-auto w-full max-w-[360px] py-10">
            <h1 className="title-display text-[28px]">{title}</h1>
            {subtitle ? <p className="mt-1.5 text-[13.5px] text-muted">{subtitle}</p> : null}
            <div className="mt-7">{children}</div>
            {footer ? <div className="mt-6 text-[13px] text-muted">{footer}</div> : null}
          </div>
        </div>
      </div>
      <aside className="relative hidden overflow-hidden border-l border-line bg-surface lg:flex lg:flex-col lg:justify-end lg:p-14">
        <SignalField />
        <div className="relative max-w-md">
          <p className="title-display text-[34px] leading-[1.08]">
            Every call ends up
            <br />
            on the lead it belongs to.
          </p>
          <p className="mt-4 text-[14px] text-muted">
            Voice agents qualify and follow up. Outcomes, transcripts and stage changes land on one timeline your team can
            act on.
          </p>
        </div>
      </aside>
    </div>
  );
}

/** Quiet field of stage-track meters — the product's visual signature, purely decorative here. */
function SignalField() {
  const rows = [5, 3, 4, 2, 5, 1, 3, 4, 2, 3, 5, 4, 1, 4, 3, 5, 2, 3, 4, 1, 5, 2, 3, 4, 3, 1, 4, 5, 2, 3];
  return (
    <div aria-hidden className="absolute inset-x-14 top-14 grid grid-cols-3 gap-x-10 gap-y-5 opacity-80 [mask-image:linear-gradient(to_bottom,black_35%,transparent_75%)]">
      {rows.map((filled, r) => (
        <div key={r} className="flex items-center gap-3">
          <div className="flex gap-[3px]">
            {Array.from({ length: 5 }, (_, i) => (
              <span
                key={i}
                className={`h-3.5 w-2 rounded-[1.5px] ${i < filled ? (filled === 5 ? "bg-success/70" : "bg-accent/60") : "bg-line-strong/60"}`}
              />
            ))}
          </div>
          <span className="h-2 flex-1 rounded-full bg-surface-2" />
        </div>
      ))}
    </div>
  );
}
