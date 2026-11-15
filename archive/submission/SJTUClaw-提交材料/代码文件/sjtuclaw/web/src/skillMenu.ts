export type SkillMenuItem = { skillId: string };

export const AUTOMATIC_SKILL_IDS = new Set(["pdf", "presentations"]);

export const manualSkillOptions = <T extends SkillMenuItem>(skills: readonly T[]): T[] =>
  skills.filter((skill) => !AUTOMATIC_SKILL_IDS.has(skill.skillId));
