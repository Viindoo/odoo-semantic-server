// SPDX-License-Identifier: AGPL-3.0-or-later
/// <reference types="astro/client" />

declare namespace App {
  interface Locals {
    user: { username: string; is_admin: boolean } | null;
  }
}
