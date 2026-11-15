import { describe, expect, it } from "vitest";

import { manualSkillOptions } from "./skillMenu";

describe("manualSkillOptions", () => {
  it("hides only default artifact Skills", () => {
    expect(
      manualSkillOptions([
        { skillId: "pdf" },
        { skillId: "presentations" },
        { skillId: "course-report" },
      ]).map((item) => item.skillId),
    ).toEqual(["course-report"]);
  });

  it("keeps unavailable and user-provided Skills visible", () => {
    const unavailable = { skillId: "paper-mindmap-report-ppt", available: false };
    expect(manualSkillOptions([unavailable])).toEqual([unavailable]);
  });
});
