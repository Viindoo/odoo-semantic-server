/// <reference types="astro/client" />

declare namespace App {
  interface Locals {
    user: { username: string; is_admin: boolean } | null;
  }
}
