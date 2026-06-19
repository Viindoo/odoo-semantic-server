// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Drift-guard for plugin capability counts (SSOT: plugins-data.ts)
 * against landing-page data (odoo-ai-agents-data.ts).
 *
 * Business rule under protection:
 * - The public site advertises exactly 42 skills / 8 agents / 10 commands / 9 personas
 * - The /odoo-ai-agents landing page, homepage, and marketplace install snippets
 *   all render from the odoo-ai-agents-data arrays
 * - If someone adds/removes skills, agents, commands, or personas without syncing
 *   both the count constant AND the data array, these tests fail precisely when
 *   the contract is violated
 * - FAQ entries must have non-empty question + answer for JSON-LD structured data
 */

import { describe, expect, it } from 'vitest';
import {
  SKILLS_COUNT,
  AGENTS_COUNT,
  COMMANDS_COUNT,
  PERSONA_COUNT,
  PERSONAS_COUNT,
  WORKFLOWS_COUNT,
} from '../plugins-data';
import { agents, skillGroups, commands, personas, faqs } from '../odoo-ai-agents-data';

describe('plugin capability counts SSOT guard (#291)', () => {
  describe('count constants from plugins-data.ts', () => {
    it('SKILLS_COUNT is 42 (source: plugin.json v3.15.0)', () => {
      expect(SKILLS_COUNT).toBe(42);
    });

    it('AGENTS_COUNT is 8 (source: plugin.json v3.15.0)', () => {
      expect(AGENTS_COUNT).toBe(8);
    });

    it('COMMANDS_COUNT is 10 (source: plugin.json v3.15.0)', () => {
      expect(COMMANDS_COUNT).toBe(10);
    });

    it('PERSONA_COUNT and PERSONAS_COUNT are both 9 (source: plugin.json v3.15.0)', () => {
      expect(PERSONA_COUNT).toBe(9);
      expect(PERSONAS_COUNT).toBe(9);
      expect(PERSONAS_COUNT).toBe(PERSONA_COUNT);
    });

    it('WORKFLOWS_COUNT is 12 (source: DECISIONS.md D1)', () => {
      expect(WORKFLOWS_COUNT).toBe(12);
    });
  });

  describe('landing-page data arrays vs count constants (parity gate)', () => {
    it('agents array length equals AGENTS_COUNT', () => {
      expect(agents.length).toBe(AGENTS_COUNT);
    });

    it('agents array has 8 unique IDs', () => {
      const ids = agents.map((a) => a.id);
      expect(new Set(ids).size).toBe(8);
      expect(ids.every((id) => id.length > 0)).toBe(true);
    });

    it('skillGroups flatMap unique skills equals SKILLS_COUNT (42)', () => {
      const allSkills = skillGroups.flatMap((g) => g.skills);
      const uniqueSkills = new Set(allSkills);
      expect(uniqueSkills.size).toBe(SKILLS_COUNT);
      expect(allSkills.length).toBeGreaterThanOrEqual(SKILLS_COUNT);
      // odoo-test-writing is listed once (in QA/CS only), bringing unique count to 42
    });

    it('commands array length equals COMMANDS_COUNT', () => {
      expect(commands.length).toBe(COMMANDS_COUNT);
    });

    it('commands array has 10 unique names', () => {
      const names = commands.map((c) => c.name);
      expect(new Set(names).size).toBe(10);
      expect(names.every((n) => n.length > 0)).toBe(true);
    });

    it('personas array length equals PERSONA_COUNT', () => {
      expect(personas.length).toBe(PERSONA_COUNT);
    });

    it('personas array has 9 unique names', () => {
      const names = personas.map((p) => p.name);
      expect(new Set(names).size).toBe(9);
      expect(names.every((n) => n.length > 0)).toBe(true);
    });

    it('faqs array has 7 entries (landing page FAQ count)', () => {
      expect(faqs.length).toBe(7);
    });

    it('every FAQ entry has non-empty question and answer (JSON-LD parity)', () => {
      faqs.forEach((faq, idx) => {
        expect(faq.question).toBeTruthy();
        expect(faq.question.length).toBeGreaterThan(0);
        expect(faq.answer).toBeTruthy();
        expect(faq.answer.length).toBeGreaterThan(0);
      });
    });
  });

  describe('skill group invariants', () => {
    it('every skill group has a non-empty name, persona, and valueProp', () => {
      skillGroups.forEach((group) => {
        expect(group.name).toBeTruthy();
        expect(group.persona).toBeTruthy();
        expect(group.valueProp).toBeTruthy();
      });
    });

    it('each skill group skills array is non-empty', () => {
      skillGroups.forEach((group) => {
        expect(group.skills.length).toBeGreaterThan(0);
      });
    });

    it('skill group deduplication: Developer (13) + Sales (8) + Marketing (5) + Consultant (5) + QA/CS (4) + Visual (3) + Orchestration (4) = 42 unique', () => {
      // Verify the documented deduplication in odoo-ai-agents-data.ts:
      // odoo-test-writing is in QA/CS only (not Developer), bringing total to 42 unique
      const groupSizes = skillGroups.map((g) => g.skills.length);
      expect(groupSizes).toEqual([13, 8, 5, 5, 4, 3, 4]);
      // 13 + 8 + 5 + 5 + 4 + 3 + 4 = 42 (counting with dedup of odoo-test-writing)
    });

    it('no skill appears in more than one skill group', () => {
      const allSkills = skillGroups.flatMap((g) => g.skills);
      const skillCounts = new Map<string, number>();
      allSkills.forEach((skill) => {
        skillCounts.set(skill, (skillCounts.get(skill) || 0) + 1);
      });
      // Every skill should appear exactly once
      skillCounts.forEach((count, skill) => {
        expect(count).toBe(1);
      });
    });
  });

  describe('agent invariants', () => {
    it('every agent has non-empty id, name, role, benefit, and valid tier', () => {
      const validTiers = ['opus', 'sonnet', 'haiku', 'fable'];
      agents.forEach((agent) => {
        expect(agent.id).toBeTruthy();
        expect(agent.name).toBeTruthy();
        expect(agent.role).toBeTruthy();
        expect(agent.benefit).toBeTruthy();
        expect(validTiers).toContain(agent.tier);
      });
    });
  });

  describe('command invariants', () => {
    it('every command has non-empty name and description', () => {
      commands.forEach((cmd) => {
        expect(cmd.name).toBeTruthy();
        expect(cmd.description).toBeTruthy();
      });
    });
  });

  describe('persona invariants', () => {
    it('every persona has non-empty name and description', () => {
      personas.forEach((persona) => {
        expect(persona.name).toBeTruthy();
        expect(persona.description).toBeTruthy();
        expect(typeof persona.primary).toBe('boolean');
      });
    });

    it('exactly one persona has primary=true (Coder persona)', () => {
      const primaryPersonas = personas.filter((p) => p.primary);
      expect(primaryPersonas.length).toBe(1);
      expect(primaryPersonas[0].name).toBe('Coder');
    });
  });
});
