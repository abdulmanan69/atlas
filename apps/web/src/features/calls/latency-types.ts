/** Latency types from the generated API schema (ADR 0006 contract), under short names. */
import type { Schemas } from "@/lib/api";

export type LatencySummary = Schemas["LatencySummary"];
export type LatencyTool = Schemas["LatencyTool"];
export type LatencyTurn = Schemas["LatencyTurn"];
export type LatencyThresholds = Required<Schemas["LatencyThresholds"]>;
export type CallLatencyDetail = Schemas["CallLatencyDetail"];
export type ResponseSpeed = NonNullable<Schemas["CallSettings"]["response_speed"]>;
export type LatencyGuide = Schemas["LatencyGuide"];
export type LatencyGuideProvider = Schemas["LatencyGuideProvider"];
export type LatencyPreset = Schemas["LatencyPreset"];
export type PresetChoice = Schemas["PresetChoice"];
export type PipelineHealth = Schemas["PipelineHealth"];
