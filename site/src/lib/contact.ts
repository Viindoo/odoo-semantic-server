// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT for public-facing contact / support URLs.
 *
 *  WI-5/6 will fetch the live value from GET /api/site-config (which reads the
 *  `support.helpdesk_url` app-setting). This constant is the fallback used when
 *  that fetch fails (network error, server cold-start, etc.). */

export const HELPDESK_URL_FALLBACK = 'https://viindoo.com/ticket/team/88';
