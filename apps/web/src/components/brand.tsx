import { MARK, WORDMARK } from "@/components/brand-paths";
import { cn } from "@/lib/utils";

/** Product name shown to assistive technology; the artwork itself is the visible brand. */
export const PRODUCT_NAME = "ATLAS";

/**
 * The ATLAS wordmark as an inline SVG. It takes the current text colour, so it is ink on
 * light surfaces and white on dark ones with no second asset. Size it with a height class;
 * the width follows the artwork's aspect ratio.
 */
export function Wordmark({ className, title = PRODUCT_NAME }: { className?: string; title?: string | null }) {
  return (
    <svg
      viewBox={WORDMARK.viewBox}
      className={cn("h-4 w-auto shrink-0 fill-current", className)}
      role={title ? "img" : undefined}
      aria-label={title ?? undefined}
      aria-hidden={title ? undefined : true}
    >
      <path fillRule="evenodd" d={WORDMARK.d} />
    </svg>
  );
}

/** The "A" mark alone, for tight spaces such as the collapsed sidebar. */
export function Mark({ className, title = PRODUCT_NAME }: { className?: string; title?: string | null }) {
  return (
    <svg
      viewBox={MARK.viewBox}
      className={cn("h-5 w-auto shrink-0 fill-current", className)}
      role={title ? "img" : undefined}
      aria-label={title ?? undefined}
      aria-hidden={title ? undefined : true}
    >
      <path fillRule="evenodd" d={MARK.d} />
    </svg>
  );
}
