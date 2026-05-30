// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared SSOT types for admin + tenant settings pages and islands (issue #217).

export type DataType = 'int' | 'float' | 'str' | 'bool' | 'duration_seconds' | 'list_str' | 'struct';

export interface SettingDef {
  key: string;
  value: unknown;
  default_value: unknown;
  data_type: DataType;
  validation: { min?: number; max?: number; enum?: string[]; regex?: string } | null;
  description: string;
  requires_restart: boolean;
  requires_reseed: boolean;
  is_secret: boolean;
  tenant_scopable: boolean;
  effective_source?: string;
  updated_at?: string | null;
  updated_by?: number | null;
  change_reason?: string | null;
}

export interface TenantSettingDef {
  key: string;
  effective_value: unknown;
  effective_source: 'tenant_override' | 'system_or_default';
  tenant_override: unknown;
  system_default: unknown;
  category: string;
  data_type: DataType;
  validation: { min?: number; max?: number; enum?: string[]; regex?: string } | null;
  description: string;
  updated_at: string | null;
  updated_by: number | null;
  change_reason: string | null;
}
