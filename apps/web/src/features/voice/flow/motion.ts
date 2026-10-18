/** Honour the person's reduced-motion preference for canvas animations. */
export const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
