import { describe, expect, it } from "vitest";
import { applySuggestion, commandSuggestions, moveSuggestion } from "./commands";

const definitions = [
  { template: "/command", description: "List local commands.", skillArgument: false },
  { template: "/memory add <content>", description: "Create memory.", skillArgument: false },
  { template: "/skill show <skill-name>", description: "Show Skill.", skillArgument: true },
  { template: "/skill <skill-name> <task>", description: "Run Skill.", skillArgument: true },
];

const skills = [
  { skillId: "course-report", available: true },
  { skillId: "presentation-outline", available: true },
  { skillId: "broken-skill", available: false },
];

describe("commandSuggestions", () => {
  it("expands dynamic Skill IDs without hard-coding presentation names", () => {
    expect(commandSuggestions(definitions, skills, "/skill show c")).toEqual([
      {
        value: "/skill show course-report",
        description: "Show Skill.",
      },
    ]);
    expect(commandSuggestions(definitions, skills, "/skill p")).toEqual([
      {
        value: "/skill presentation-outline ",
        description: "Run Skill.",
      },
    ]);
  });

  it("filters command templates and ignores normal chat text", () => {
    expect(commandSuggestions(definitions, skills, "/mem")).toEqual([
      {
        value: "/memory add ",
        description: "Create memory.",
      },
    ]);
    expect(commandSuggestions(definitions, skills, "summarize this")).toEqual([]);
  });

  it("wraps keyboard selection and applies the selected template", () => {
    expect(moveSuggestion(0, -1, 3)).toBe(2);
    expect(moveSuggestion(2, 1, 3)).toBe(0);
    expect(moveSuggestion(0, 1, 0)).toBe(-1);
    expect(applySuggestion("/mem", "/memory add ")).toBe("/memory add ");
  });
});
