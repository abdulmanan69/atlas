import { Slider as SliderPrimitive } from "radix-ui";
import { forwardRef, type ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface SliderProps extends Omit<ComponentProps<typeof SliderPrimitive.Root>, "value" | "defaultValue" | "onValueChange" | "onValueCommit"> {
  value: number;
  onValueChange: (value: number) => void;
  /** Called once when the person lets go (pointer up or key press). */
  onValueCommit?: (value: number) => void;
  /** Accessible name of the thumb. */
  label: string;
  /** Spoken value, e.g. "40%". */
  valueText?: string;
}

/** Single-value range input (volume, thresholds). Keyboard: arrows, Page Up/Down, Home/End. */
export const Slider = forwardRef<HTMLSpanElement, SliderProps>(({ value, onValueChange, onValueCommit, label, valueText, className, ...props }, ref) => (
  <SliderPrimitive.Root
    ref={ref}
    value={[value]}
    onValueChange={(v) => onValueChange(v[0] ?? value)}
    onValueCommit={onValueCommit ? (v) => onValueCommit(v[0] ?? value) : undefined}
    className={cn("relative flex h-5 w-full touch-none select-none items-center data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50", className)}
    {...props}
  >
    <SliderPrimitive.Track className="relative h-1.5 grow overflow-hidden rounded-full bg-line-strong">
      <SliderPrimitive.Range className="absolute h-full rounded-full bg-accent" />
    </SliderPrimitive.Track>
    <SliderPrimitive.Thumb
      aria-label={label}
      aria-valuetext={valueText}
      className="block size-4 rounded-full border border-accent bg-white shadow-card transition-shadow focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-accent/25"
    />
  </SliderPrimitive.Root>
));
Slider.displayName = "Slider";
