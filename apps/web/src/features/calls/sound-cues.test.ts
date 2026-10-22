import { cueLabel, cueTag, soundSummary, splitSoundCues } from "@/features/calls/sound-cues";

describe("sound cues", () => {
  it("picks out the sounds an expressive voice makes, in any spelling a model writes", () => {
    expect(splitSoundCues("[laugh] That's great! Oh [Sighs] okay.")).toEqual([
      { kind: "cue", tag: "laugh", label: "laughs" },
      { kind: "text", text: " That's great! Oh " },
      { kind: "cue", tag: "sigh", label: "sighs" },
      { kind: "text", text: " okay." },
    ]);
    expect(splitSoundCues("Well [ clears  throat ]")).toEqual([
      { kind: "text", text: "Well " },
      { kind: "cue", tag: "clear throat", label: "clears throat" },
    ]);
    expect(cueTag("laughter")).toBe("laugh");
    expect(cueTag("shh")).toBe("shush");
    expect(cueLabel("chuckle")).toBe("chuckles");
  });

  it("leaves other bracketed text as written", () => {
    expect(splitSoundCues("[SIMULATED CALL]")).toEqual([{ kind: "text", text: "[SIMULATED CALL]" }]);
    expect(splitSoundCues("Press [1] or say [constructor].")).toEqual([{ kind: "text", text: "Press [1] or say [constructor]." }]);
    expect(splitSoundCues("")).toEqual([]);
  });

  it("names what a voice can do in two sounds at most", () => {
    expect(soundSummary(["laugh", "chuckle", "sigh", "gasp", "cough"])).toBe("Laughs, sighs");
    expect(soundSummary(["gasp", "cough"])).toBe("Gasps, coughs");
    expect(soundSummary(["yawn"])).toBe("Makes sounds");
  });
});
