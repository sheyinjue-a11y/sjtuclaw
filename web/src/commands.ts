export type CommandDefinition = {
  template: string;
  description: string;
  skillArgument: boolean;
};

export type CommandSkill = {
  skillId: string;
  available: boolean;
};

export type CommandSuggestion = {
  value: string;
  description: string;
};

export function commandSuggestions(
  definitions: CommandDefinition[],
  skills: CommandSkill[],
  input: string,
): CommandSuggestion[] {
  const query = input;
  if (!query.startsWith("/")) return [];
  const normalized = query.toLocaleLowerCase();
  const suggestions: CommandSuggestion[] = [];

  for (const definition of definitions) {
    if (definition.skillArgument && definition.template === "/skill show <skill-name>") {
      suggestions.push(...skillShowSuggestions(definition, skills, query));
      continue;
    }
    if (definition.skillArgument && definition.template === "/skill <skill-name> <task>") {
      suggestions.push(...skillRunSuggestions(definition, skills, query));
      continue;
    }
    const value = completionValue(definition.template);
    if (value.toLocaleLowerCase().startsWith(normalized)) {
      suggestions.push({ value, description: definition.description });
    }
  }
  return suggestions;
}

export function moveSuggestion(index: number, delta: number, count: number): number {
  if (count < 1) return -1;
  return (index + delta + count) % count;
}

export function applySuggestion(_input: string, value: string): string {
  return value;
}

function skillShowSuggestions(
  definition: CommandDefinition,
  skills: CommandSkill[],
  query: string,
): CommandSuggestion[] {
  const prefix = "/skill show ";
  if (!query.toLocaleLowerCase().startsWith(prefix)) return [];
  const filter = query.slice(prefix.length).trim().toLocaleLowerCase();
  return availableSkills(skills)
    .filter((skill) => skill.skillId.toLocaleLowerCase().startsWith(filter))
    .map((skill) => ({ value: `${prefix}${skill.skillId}`, description: definition.description }));
}

function skillRunSuggestions(
  definition: CommandDefinition,
  skills: CommandSkill[],
  query: string,
): CommandSuggestion[] {
  const prefix = "/skill ";
  if (!query.toLocaleLowerCase().startsWith(prefix)) return [];
  const value = query.slice(prefix.length);
  if (value.trimStart().includes(" ") || value.trimStart().startsWith("show")) return [];
  const filter = value.trim().toLocaleLowerCase();
  return availableSkills(skills)
    .filter((skill) => skill.skillId.toLocaleLowerCase().startsWith(filter))
    .map((skill) => ({ value: `${prefix}${skill.skillId} `, description: definition.description }));
}

function availableSkills(skills: CommandSkill[]): CommandSkill[] {
  return skills.filter((skill) => skill.available);
}

function completionValue(template: string): string {
  return template.replace(/<[^>]+>/g, "");
}
