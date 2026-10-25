import { fetchJSON, BASE } from "./client";


export interface RunningApp {
  process_name: string;
  display_name: string;
  /** Best-effort PNG data-URI of the app's icon (`"data:image/png;base64,..."`),
   *  or null when the backend couldn't extract one. The picker renders this as a
   *  small `<img>` and falls back to a generic glyph when null. */
  icon: string | null;
  /** Full exe path when known (running apps + installed apps expose it; null when
   *  unavailable). Recorded into `power_mode_paths` when the app is added so
   *  install state can later be checked by path existence. */
  path?: string | null;
}


/** Install-resolution result for a mapped process name. `installed=false` means
 *  the app isn't on this machine (running, in App Paths, a Store alias, or a
 *  Start Menu shortcut); the profile editor hides those rules. `display_name`
 *  is the system friendly name (exe FileDescription); `icon` is extracted from
 *  the resolved on-disk exe so installed-but-closed apps still get a real icon. */
export interface ResolvedApp {
  process_name: string;
  installed: boolean;
  display_name: string;
  icon: string | null;
  /** Full exe path when known (resolve-path / installed resolution expose it).
   *  Used to record `power_mode_paths` when a browsed app is added. */
  path?: string | null;
}


export async function getRunningApps(): Promise<RunningApp[]> {
  return fetchJSON(`${BASE}/api/apps/running`);
}


/** Resolve install state + system name + icon for a set of mapped process
 *  names. The backend never raises (returns [] on failure); callers treat a
 *  missing entry as "not yet resolved" and hide the rule until it is. Routing
 *  (`power_mode_map`) is never modified by this call. */
export async function resolveApps(names: string[]): Promise<ResolvedApp[]> {
  return fetchJSON(`${BASE}/api/apps/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
}


/** The full set of installed apps for the picker (Start Menu + App Paths +
 *  Uninstall registry). Shaped like RunningApp so the picker can merge it with
 *  the running list; every entry is installed by construction. The backend
 *  module-caches the list and never raises (returns [] on failure). */
export async function getInstalledApps(): Promise<RunningApp[]> {
  return fetchJSON(`${BASE}/api/apps/installed`);
}


/** Resolve a user-browsed executable path to an app record (process name, system
 *  name, icon), or null for a missing / non-exe path. Backs the picker's
 *  "Browse..." affordance; the backend never raises. */
export async function resolveAppByPath(path: string): Promise<ResolvedApp | null> {
  return fetchJSON(`${BASE}/api/apps/resolve-path`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
}


/** A site-rule favicon result. `icon` is a data-URI read LOCALLY from the
 *  installed browsers' favicon caches (no network call), or null when no cached
 *  favicon exists for the domain. */
export interface SiteIcon {
  domain: string;
  icon: string | null;
}


/** Resolve favicons for a set of routed domains. The backend reads them from
 *  the browser's local favicon cache only (never the network) and never raises
 *  (returns [] on failure); callers treat a missing entry as "no favicon" and
 *  render the generic globe glyph. Domains are lowercased server-side. */
export async function resolveSiteIcons(domains: string[]): Promise<SiteIcon[]> {
  return fetchJSON(`${BASE}/api/sites/icons`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ domains }),
  });
}
