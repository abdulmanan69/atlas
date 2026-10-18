import { Switch as SwitchPrimitive } from "radix-ui";
import { forwardRef, type ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface SwitchProps extends Omit<ComponentProps<typeof SwitchPrimitive.Root>, "onCheckedChange" | "checked"> {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  size?: "sm" | "md";
}

/** On/off setting that applies immediately. Use a Checkbox inside forms that are submitted. */
export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(({ className, size = "md", ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "group relative inline-flex shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-line-strong p-px transition-colors",
      "data-[state=checked]:bg-accent disabled:cursor-not-allowed disabled:opacity-50",
      size === "sm" ? "h-4 w-7" : "h-5 w-9",
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={cn(
        "block rounded-full bg-white shadow-card transition-transform",
        size === "sm" ? "size-3 data-[state=checked]:translate-x-3" : "size-4 data-[state=checked]:translate-x-4",
      )}
    />
  </SwitchPrimitive.Root>
));
Switch.displayName = "Switch";
