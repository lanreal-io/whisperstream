import { useEffect, useMemo, useState } from "react";
import { getRunningApps, getInstalledApps, resolveApps, resolveSiteIcons, type ResolvedApp, type RunningApp } from "../../lib/api/apps";
import type { AppConfig } from "../../lib/api/config";

const LEGACY_POWER_CACHE_KEYS = [
  "ws.powermode.resolvedApps.v1",
  "ws.powermode.siteIcons.v1",
] as const;

export function useRoutingInventory({ addAppOpen, appIdentityEnabled, browserEnabled, powerModeMap, siteMap }: {
  addAppOpen: boolean;
  appIdentityEnabled: boolean;
  browserEnabled: boolean;
  powerModeMap: AppConfig["power_mode_map"];
  siteMap: AppConfig["site_map"];
}) {
  // Picker inventories are needed only while Add App is open. Fetch a fresh
  // snapshot for each open, ignore late responses after close, and release the
  // process/path/icon records as soon as the modal closes.
  const [runningApps, setRunningApps] = useState<RunningApp[]>([]);
  const [runningLoading, setRunningLoading] = useState(false);
  const [installedApps, setInstalledApps] = useState<RunningApp[]>([]);
  useEffect(() => {
    if (!addAppOpen || !appIdentityEnabled) {
      setRunningApps([]);
      setInstalledApps([]);
      setRunningLoading(false);
      return;
    }

    let cancelled = false;
    setRunningApps([]);
    setInstalledApps([]);
    setRunningLoading(true);
    getRunningApps()
      .then((apps) => {
        if (!cancelled) setRunningApps(Array.isArray(apps) ? apps : []);
      })
      .catch(() => {
        if (!cancelled) setRunningApps([]);
      })
      .finally(() => {
        if (!cancelled) setRunningLoading(false);
      });
    getInstalledApps()
      .then((apps) => {
        if (!cancelled) setInstalledApps(Array.isArray(apps) ? apps : []);
      })
      .catch(() => {
        if (!cancelled) setInstalledApps([]);
      });

    return () => {
      cancelled = true;
    };
  }, [addAppOpen, appIdentityEnabled]);

  // Retire the two restart-persistent identity/icon caches from older builds.
  // WebView storage can be unavailable, so cleanup remains best-effort.
  useEffect(() => {
    try {
      for (const key of LEGACY_POWER_CACHE_KEYS) localStorage.removeItem(key);
    } catch {
      // Memory-only resolution below still works when storage is unavailable.
    }
  }, []);

  // Install-resolution for the mapped process names (issues 1, 2, 4). Keyed by
  // lowercased process_name. An uninstalled app resolves to installed=false and
  // is filtered out of the profile rule lists; an installed-but-closed app gets
  // a real system name + on-disk icon. Re-runs whenever the SET of mapped keys
  // changes (a new rule added, one removed) and on mount; routing is read-only
  // here. The backend degrades to [] on failure, so the .catch leaves the map
  // empty (rules stay hidden until resolution succeeds — honors "don't show
  // uninstalled").
  const [resolvedApps, setResolvedApps] = useState<Map<string, ResolvedApp>>(
    () => new Map(),
  );
  const mappedKeysSignature = useMemo(
    () =>
      Object.keys(powerModeMap ?? {})
        .map((k) => k.toLowerCase())
        .sort()
        .join("\n"),
    [powerModeMap],
  );
  useEffect(() => {
    const names = mappedKeysSignature ? mappedKeysSignature.split("\n") : [];
    if (!appIdentityEnabled || names.length === 0) {
      setResolvedApps(new Map());
      return;
    }
    const requested = new Set(names);
    setResolvedApps(
      (current) =>
        new Map([...current].filter(([key]) => requested.has(key.toLowerCase()))),
    );
    let cancelled = false;
    resolveApps(names)
      .then((apps) => {
        if (cancelled) return;
        const next = new Map<string, ResolvedApp>();
        for (const a of Array.isArray(apps) ? apps : []) {
          next.set(a.process_name.toLowerCase(), a);
        }
        setResolvedApps(next);
      })
      .catch(() => {
        // Keep only still-requested in-memory results on a transient failure.
      });
    return () => {
      cancelled = true;
    };
  }, [mappedKeysSignature, appIdentityEnabled]);

  // Local favicons for routed site rules, read from the installed browser cache
  // (no network). Mirrors resolvedApps; a missing/loading domain falls back to
  // the globe glyph in SiteGlyph.
  const [siteIcons, setSiteIcons] = useState<Map<string, string | null>>(
    () => new Map(),
  );
  const siteKeysSignature = useMemo(
    () =>
      [...new Set(Object.keys(siteMap ?? {})
        .map((k) => k.trim().toLowerCase()))]
        .sort()
        .join("\n"),
    [siteMap],
  );
  useEffect(() => {
    const domains = siteKeysSignature ? siteKeysSignature.split("\n") : [];
    if (!browserEnabled || domains.length === 0) {
      setSiteIcons(new Map());
      return;
    }
    const requested = new Set(domains);
    setSiteIcons(
      (current) =>
        new Map([...current].filter(([key]) => requested.has(key.toLowerCase()))),
    );
    let cancelled = false;
    void (async () => {
      // Bound each request and outstanding work; a failed batch cannot erase hits.
      for (let offset = 0; offset < domains.length && !cancelled; offset += 128) {
        try {
          const rows = await resolveSiteIcons(domains.slice(offset, offset + 128));
          if (cancelled) return;
          setSiteIcons((current) => {
            const next = new Map(current);
            for (const row of Array.isArray(rows) ? rows : []) {
              const key = row.domain.toLowerCase();
              if (requested.has(key)) next.set(key, row.icon);
            }
            return next;
          });
        } catch {
          // Preserve current requested results and continue with other batches.
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [siteKeysSignature, browserEnabled]);

  return { runningApps, installedApps, runningLoading, resolvedApps, siteIcons };
}
