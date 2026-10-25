import { useState, useEffect, useCallback } from "react";
import { AnimatePresence } from "framer-motion";
import type { AppConfig } from "../../lib/api/config";
import type { AppEvent } from "../../lib/socket/events";
import type { EnhancementModelCapabilities } from "../../lib/api/enhancement";
import { type AdvancedCapabilityReport, advancedCapabilityDecision } from "../../lib/advancedCapabilities";
import { type Profile, freshProfileId, addProfile as addProfileReducer, removeProfile as removeProfileReducer, makeDefault as makeDefaultReducer, isBuiltinId } from "../../lib/profiles";
import type { ProfileIconKey } from "../../components/profileIcons";
import { type RoutedTarget, ProfileEditorModal } from "../../components/ProfileEditorModal";
import type { PromptTesterGate } from "../../components/PromptTester";
import { AddAppModal } from "../../components/AddAppModal";
import { SecondaryButton } from "../../components/SecondaryButton";
import { normalizeDomain } from "../../lib/normalizeDomain";
import { resolveActiveSttArtifact } from "../../lib/languages";
import { ProfileRow } from "./ProfileRow";
import { PlusIcon } from "./Glyphs";
import { useRoutingInventory } from "./useRoutingInventory";

interface ProfilesSectionProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  send: (msg: Record<string, unknown>) => boolean;
  connected: boolean;
  advancedCapabilities: AdvancedCapabilityReport;
  testerGate: PromptTesterGate;
  activeModelCapabilities: EnhancementModelCapabilities;
  /** Display name of the active local model ("Stream-1 Mini"), used in the
   *  editor's fixed-prompt guidance. */
  activeModelLabel: string;
  activeProvider: string;
  reduceMotion: boolean;
  /** Close any editor, then open the local model picker on the page. */
  onChangeModel: () => void;
}

/** Page-private owner for profile drafts, editing and routing. App remains the
 * only config writer; create/delete and rule batches each emit one atomic patch.
 * Credentials, provider consent and model lifecycle stay with Enhancement. */
export function ProfilesSection({
  config, onConfigChange, subscribe, send, connected, advancedCapabilities,
  testerGate, activeModelCapabilities, activeModelLabel, activeProvider, reduceMotion,
  onChangeModel,
}: ProfilesSectionProps) {
  const appIdentityCapability = advancedCapabilityDecision(
    advancedCapabilities,
    "app-identity",
  );
  const browserIdentityCapability = advancedCapabilityDecision(
    advancedCapabilities,
    "browser-page-identity",
  );
  const appPathBrowseCapability = advancedCapabilityDecision(
    advancedCapabilities,
    "app-path-browse",
  );
  const canAddPowerModeRule =
    appIdentityCapability.enabled || browserIdentityCapability.enabled;
  // Which profile is open in the editor MODAL (round 03; null = closed). The
  // editor lifted out of the row into a full-window overlay so the prompt
  // textarea + tester never fight the tab's vertical budget. `editorFocusName`
  // focuses the name field on open (the new-profile flow sets it true).
  const [editorProfileId, setEditorProfileId] = useState<string | null>(null);
  const [editorFocusName, setEditorFocusName] = useState(false);

  // Active manual-override (issue 14). Set by the transient `active_profile_override`
  // WS signal: null = no override (auto-routing); non-null = a hotkey override is
  // active, holding the switched-to profile's name for the Settings status line.
  const [activeOverride, setActiveOverride] = useState<{ profile_id: string; name: string } | null>(null);

  // Draft profile (issue 7). `+ Add profile` opens the editor on a local draft
  // that is NOT persisted until Create. The draft holds its own name + prompt
  // and its pending app/site rules locally (keyed by the same RoutedTarget
  // union) so Cancel / dismiss discards everything (no orphan "Untitled" left
  // behind). Existing profiles keep autosave-on-change. When non-null, the
  // editor renders in draft mode and the AddAppModal targets the draft, writing
  // rules into draftRules instead of power_mode_map / site_map.
  const [draft, setDraft] = useState<{
    id: string;
    name: string;
    prompt: string;
    /** Manual-switch hotkey captured locally for the draft; "" = none. Flushed
     *  into profile_hotkeys on Create. */
    hotkey: string;
    /** Per-profile input style override buffered locally for the draft. Absent
     *  (undefined) = inherit global; explicit = pin. Flushed on Create. */
    inputStyle?: "clipboard" | "keystroke";
    language?: string;
    /** Curated profile icon buffered locally for the draft. Absent = the letter
     *  fallback. Flushed onto the new profile on Create; Cancel discards it. */
    icon?: ProfileIconKey;
    /** Registry cleanup mode buffered locally for the draft. Absent
     *  = the backend default (chat). Flushed onto the new profile on Create. */
    mode?: "chat" | "code" | "email";
    /** Whether Create should also point default_profile_id at this new profile.
     *  A draft can't be the live default until it exists, so the intent is
     *  buffered here and applied in commitDraft. Absent/false = leave the
     *  current default untouched. */
    makeDefault?: boolean;
    rules: {
      kind: "app" | "site";
      key: string;
      label: string;
      icon: string | null;
      /** Full exe path for an app rule, flushed into power_mode_paths on Create. */
      path?: string | null;
    }[];
  } | null>(null);

  // Add-app MODAL state (round 03). Open + the profile the chooser defaults to.
  // The section `+ Add app` opens it targeting the current default; the editor's
  // `+ Add app` opens it pre-targeted to that profile.
  const [addAppOpen, setAddAppOpen] = useState(false);
  const [addAppTargetProfileId, setAddAppTargetProfileId] = useState<string | null>(null);

  const { runningApps, installedApps, runningLoading, resolvedApps, siteIcons } = useRoutingInventory({
    addAppOpen,
    appIdentityEnabled: appIdentityCapability.enabled,
    browserEnabled: browserIdentityCapability.enabled,
    powerModeMap: config.power_mode_map,
    siteMap: config.site_map,
  });

  // Subscribe to the transient active_profile_override signal (issue 14) so the
  // Profiles section can surface the manual-override status line. profile_id null
  // = cleared (auto-routing resumed). Carries the profile name for the message.
  useEffect(() => {
    return subscribe((event) => {
      if (event.type !== "active_profile_override") return;
      if (event.profile_id === null || event.profile_id === undefined) {
        setActiveOverride(null);
      } else {
        setActiveOverride({ profile_id: event.profile_id, name: event.name ?? event.profile_id });
      }
    });
  }, [subscribe]);

  // ---- Enhancement-profile mutations ----
  // PUT /api/config replaces the WHOLE config, so every mutation persists the
  // COMPLETE { profiles, default_profile_id } pair. `persist` merges a patch
  // onto the current pair so a partial write never drops the array. App.tsx
  // debounces (500ms) so rapid edits coalesce.
  const persistProfiles = (patch: {
    profiles?: Profile[];
    default_profile_id?: string;
  }) => {
    onConfigChange({
      profiles: patch.profiles ?? config.profiles,
      default_profile_id: patch.default_profile_id ?? config.default_profile_id,
    });
  };

  // Start a new DRAFT profile (issue 7). Nothing is persisted yet: the draft
  // lives in local state with a blank prompt (the round-03 Chat auto-seed is
  // removed — the Template dropdown is now the single explicit prefill) and an
  // empty pending-rules list. The editor opens in draft mode focused on the
  // name field. Create commits; Cancel / dismiss discards.
  const startDraftProfile = () => {
    const newId = freshProfileId(config.profiles.map((p) => p.id));
    setDraft({ id: newId, name: "New profile", prompt: "", hotkey: "", rules: [] });
    setEditorProfileId(null);
    setEditorFocusName(true);
  };

  // Commit the draft (Create): persist the new profile (name, prompt) AND flush
  // its pending rules into power_mode_map / site_map in ONE config write, so a
  // partial save never drops the array or maps. Then discard the local draft.
  const commitDraft = (name: string, prompt: string) => {
    if (!draft) return;
    const trimmed = name.trim() || "New profile";
    const next = addProfileReducer(
      { profiles: config.profiles, default_profile_id: config.default_profile_id },
      {
        id: draft.id,
        name: trimmed,
        prompt,
        builtin: false,
        ...(draft.icon ? { icon: draft.icon } : {}),
        ...(draft.inputStyle ? { input_style: draft.inputStyle } : {}),
        ...(draft.language ? { language: draft.language } : {}),
        ...(draft.mode ? { mode: draft.mode } : {}),
      },
    );
    const nextPowerMap = { ...(config.power_mode_map ?? {}) };
    const nextSiteMap = { ...(config.site_map ?? {}) };
    const nextPaths = { ...(config.power_mode_paths ?? {}) };
    for (const r of draft.rules) {
      if (r.kind === "app") {
        nextPowerMap[r.key.toLowerCase()] = draft.id;
        if (r.path) nextPaths[r.key.toLowerCase()] = r.path;
      } else {
        nextSiteMap[normalizeDomain(r.key) ?? r.key.toLowerCase()] = draft.id;
      }
    }
    // Flush the draft hotkey into profile_hotkeys (only when one was captured).
    const nextHotkeys = { ...(config.profile_hotkeys ?? {}) };
    if (draft.hotkey.trim()) {
      nextHotkeys[draft.id] = draft.hotkey;
    }
    onConfigChange({
      profiles: next.profiles,
      // Honor a "Make default" pick made during creation: point the default at
      // the new profile in the same write. addProfile never selects on its own
      // ("creating is not selecting"), so this is the only path that promotes a
      // freshly created profile to default.
      default_profile_id: draft.makeDefault ? draft.id : next.default_profile_id,
      power_mode_map: nextPowerMap,
      power_mode_paths: nextPaths,
      site_map: nextSiteMap,
      profile_hotkeys: nextHotkeys,
    });
    setDraft(null);
    setEditorFocusName(false);
  };

  // Discard the draft (Cancel / dismiss / Esc / backdrop). Leaves config
  // untouched — no orphan profile or map entries.
  const discardDraft = () => {
    setDraft(null);
    setEditorFocusName(false);
  };

  // Add a pending rule to the draft (from the AddAppModal opened on a draft).
  const addDraftRule = (kind: "app" | "site", key: string, path: string | null = null) => {
    setDraft((d) => {
      if (!d) return d;
      const normKey =
        kind === "app" ? key.toLowerCase() : normalizeDomain(key) ?? key.toLowerCase();
      // De-dupe within the draft (same key replaces; never double-adds).
      const rest = d.rules.filter((r) => !(r.kind === kind && r.key === normKey));
      const rec =
        kind === "app"
          ? { kind: "app" as const, key: normKey, path, ...appRecordForProcess(normKey) }
          : { kind: "site" as const, key: normKey, label: normKey, icon: null };
      return { ...d, rules: [...rest, rec] };
    });
  };

  // Remove a pending rule from the draft (chip ✕ in the editor).
  const removeDraftRule = (kind: "app" | "site", key: string) => {
    setDraft((d) =>
      d
        ? { ...d, rules: d.rules.filter((r) => !(r.kind === kind && r.key === key.toLowerCase())) }
        : d,
    );
  };

  // Remove a custom profile. If it was the default, the reducer reassigns the
  // default pointer to "chat" in the SAME patch so the list is never
  // default-less. Any apps routed to the removed profile are also dropped from
  // power_mode_map (they fall back to the default automatically). Persist
  // profiles + default + the pruned map in ONE patch.
  const removeProfile = (id: string) => {
    const next = removeProfileReducer(
      { profiles: config.profiles, default_profile_id: config.default_profile_id },
      id,
    );
    const prunedMap = Object.fromEntries(
      Object.entries(config.power_mode_map ?? {}).filter(([, pid]) => pid !== id),
    );
    const prunedSiteMap = Object.fromEntries(
      Object.entries(config.site_map ?? {}).filter(([, pid]) => pid !== id),
    );
    // Drop the removed profile's manual-switch hotkey too (the backend would
    // prune it on validate, but pruning here keeps the wire payload clean).
    const prunedHotkeys = { ...(config.profile_hotkeys ?? {}) };
    delete prunedHotkeys[id];
    if (editorProfileId === id) setEditorProfileId(null);
    onConfigChange({
      profiles: next.profiles,
      default_profile_id: next.default_profile_id,
      power_mode_map: prunedMap,
      site_map: prunedSiteMap,
      profile_hotkeys: prunedHotkeys,
    });
  };

  // ---- Profile prompt + rename ----
  // Persist a single profile's prompt (used by the inline editor textarea and
  // by the larger CustomPromptDialog). Rewrites the WHOLE profiles array.
  const setProfilePrompt = (id: string, prompt: string) => {
    persistProfiles({
      profiles: config.profiles.map((p) => (p.id === id ? { ...p, prompt } : p)),
    });
  };

  // Persist (or clear) a single profile's per-profile input_style override.
  // style=undefined -> DELETE the key so the profile inherits the global;
  // explicit value -> SET it, pinning the style for this profile.
  const setProfileInputStyle = (
    id: string,
    style: "clipboard" | "keystroke" | undefined,
  ) => {
    persistProfiles({
      profiles: config.profiles.map((p) => {
        if (p.id !== id) return p;
        if (style === undefined) {
          // Delete the key: spread then remove input_style so inherit takes effect.
          const { input_style: _removed, ...rest } = p;
          void _removed; // consumed to satisfy the destructure
          return rest;
        }
        return { ...p, input_style: style };
      }),
    });
  };
  // Persist (or clear) the built-in Email profile's default sign-off. Existing
  // stale signature keys on other profiles are left untouched, but this path
  // can neither create nor modify them.
  const setProfileSignature = (id: string, signature: string) => {
    if (id !== "email") return;
    persistProfiles({
      profiles: config.profiles.map((p) => {
        if (p.id !== id) return p;
        if (!signature.trim()) {
          const { signature: _removed, ...rest } = p;
          void _removed; // consumed to satisfy the destructure
          return rest;
        }
        return { ...p, signature };
      }),
    });
  };
  // Persist a single profile's fine-tune cleanup mode ("chat"|"code"). The
  // backend uses this field only for Stream-1 models; Gemma and cloud providers
  // continue to use the profile's stored free-text prompt.
  // Persist (or clear) a custom profile's curated icon. Clearing DELETES the
  // key rather than storing an empty string, so the stamp falls back to the
  // profile's first letter and the backend never sees a blank enum value.
  const setProfileIcon = (id: string, icon: ProfileIconKey | undefined) => {
    persistProfiles({
      profiles: config.profiles.map((p) => {
        if (p.id !== id) return p;
        if (icon === undefined) {
          const { icon: _removed, ...rest } = p;
          void _removed; // consumed to satisfy the destructure
          return rest;
        }
        return { ...p, icon };
      }),
    });
  };

  const setProfileMode = (id: string, mode: "chat" | "code" | "email") => {
    persistProfiles({
      profiles: config.profiles.map((p) => (p.id === id ? { ...p, mode } : p)),
    });
  };

  // Persist a single profile's name (inline rename in the editor header).
  const renameProfile = (id: string, name: string) => {
    const trimmed = name.trim();
    if (!trimmed) return;
    persistProfiles({
      profiles: config.profiles.map((p) => (p.id === id ? { ...p, name: trimmed } : p)),
    });
  };

  // Set or clear a profile's manual-switch hotkey (issue 14). `keys` is the
  // captured shortcut ("" clears it). Rewrites the WHOLE profile_hotkeys map
  // (PUT resends the full config). The backend validate() drops a hotkey that
  // collides with primary/paste-last or duplicates another profile, so a bad
  // capture is silently pruned server-side rather than rejected here.
  const setProfileHotkey = (id: string, keys: string) => {
    const next = { ...(config.profile_hotkeys ?? {}) };
    if (keys.trim()) {
      next[id] = keys;
    } else {
      delete next[id];
    }
    onConfigChange({ profile_hotkeys: next });
  };

  // Set or clear the default profile (issue 10/13). `next` true -> point the
  // default at this id; `next` false -> clear the default entirely (""), which
  // the backend reads as "no enhancement for unmapped targets" (raw inject).
  const setProfileDefault = (id: string, next: boolean) => {
    persistProfiles(
      next
        ? makeDefaultReducer(
            { profiles: config.profiles, default_profile_id: config.default_profile_id },
            id,
          )
        : { profiles: config.profiles, default_profile_id: "" },
    );
  };

  // ---- App routing (power_mode_map) ----
  // power_mode_map is process name (lowercased) -> profile id. PUT resends the
  // WHOLE config, so add/remove rebuild the complete next map; a partial write
  // would wipe every other entry. An app already mapped to ANOTHER profile is
  // MOVED here (the lowercased key is overwritten), never duplicated.
  const powerModeMap = config.power_mode_map ?? {};
  // Assigning apps is handled by the AddAppModal's batch onAdd below (it merges
  // every staged app + site into ONE config write); there is no single-app
  // assign helper anymore. Removal stays per-chip.
  const unassignApp = (processName: string) => {
    const next = { ...powerModeMap };
    delete next[processName.toLowerCase()];
    onConfigChange({ power_mode_map: next });
  };

  // Friendly label + live icon for a stored process key: prefer the resolved
  // record (system FileDescription name + on-disk icon, so installed-but-closed
  // apps still show a real icon — issues 2, 4), then the running-app record,
  // then the raw process name with no icon.
  const appRecordForProcess = useCallback(
    (processName: string): { label: string; icon: string | null } => {
      const key = processName.toLowerCase();
      const resolved = resolvedApps.get(key);
      if (resolved) {
        return {
          label: resolved.display_name || processName,
          icon: resolved.icon ?? null,
        };
      }
      const hit = runningApps.find((a) => a.process_name.toLowerCase() === key);
      return { label: hit?.display_name || processName, icon: hit?.icon ?? null };
    },
    [resolvedApps, runningApps],
  );
  // The apps routed to a given profile, as { processName, label, icon } records.
  // Install-gated (issue 1): an app resolved as installed=false is FILTERED OUT;
  // an app not yet resolved (map miss during the in-flight load) is ALSO hidden
  // until resolution confirms it is installed ("don't show uninstalled"). The
  // user's power_mode_map is NEVER mutated here — gating is purely cosmetic, so
  // an app installed later auto-appears once it resolves.
  const appsForProfile = useCallback(
    (profileId: string): { processName: string; label: string; icon: string | null }[] =>
      Object.entries(powerModeMap)
        .filter(([, pid]) => pid === profileId)
        .filter(([processName]) => {
          const resolved = resolvedApps.get(processName.toLowerCase());
          return resolved ? resolved.installed : false;
        })
        .map(([processName]) => ({ processName, ...appRecordForProcess(processName) })),
    [powerModeMap, resolvedApps, appRecordForProcess],
  );

  // ---- Site routing (site_map) ----
  // site_map is registrable domain (lowercased) -> profile id. Mirrors the app
  // helpers above. A domain already mapped to ANOTHER profile is MOVED here
  // (overwrite-on-commit, matching app behavior).
  const siteMap = config.site_map ?? {};
  // Site assignment is likewise batched through the AddAppModal's onAdd below.
  const unassignSite = (domain: string) => {
    const next = { ...siteMap };
    delete next[domain.toLowerCase()];
    onConfigChange({ site_map: next });
  };
  // Sites routed to a given profile, as { kind:"site", domain, label } records.
  const sitesForProfile = useCallback(
    (profileId: string): { kind: "site"; domain: string; label: string; icon: string | null }[] =>
      Object.entries(siteMap)
        .filter(([, pid]) => pid === profileId)
        .map(([domain]) => ({
          kind: "site" as const,
          domain,
          label: domain,
          icon: siteIcons.get(domain.toLowerCase()) ?? null,
        })),
    [siteMap, siteIcons],
  );

  // Merge per profile: apps (tagged kind:"app") then sites (tagged kind:"site").
  // Keys are unique: app:<process> / site:<domain> so a process name and domain
  // can't collide.
  const targetsForProfile = useCallback(
    (profileId: string) => [
      ...appsForProfile(profileId).map((a) => ({ kind: "app" as const, ...a })),
      ...sitesForProfile(profileId),
    ],
    [appsForProfile, sitesForProfile],
  );

  const activeModelPromptFixed = activeProvider === "local" && !activeModelCapabilities.editable_prompt;
  const activeModelTrainedModes = (activeModelCapabilities.supported_modes ?? ["chat", "code"])
    .filter((mode): mode is "chat" | "code" | "email" =>
      mode === "chat" || mode === "code" || mode === "email",
    );

  return (
    <>
        <div className="flex items-center justify-between mb-1">
          <div className="text-[13px] font-medium text-slate">Profiles</div>
          <SecondaryButton
            onClick={startDraftProfile}
            className="inline-flex items-center gap-1 h-8 px-2.5 rounded-full text-[12px] font-medium text-slate"
          >
            <PlusIcon />
            Add profile
          </SecondaryButton>
        </div>
        <div className="text-[12px] text-steel mb-3">
          Each app or website dictates through its matching profile.
        </div>

        {/* Active manual-override status line (issue 14). Shows while a hotkey
            override is in effect; "Resume auto-routing" clears it via the WS
            profile.override.clear message (the backend resets _manual_profile_id
            and broadcasts the cleared state, which this component subscribes to). */}
        {activeOverride && (
          <div className="flex items-center gap-2 flex-wrap mb-3 text-[11px] text-slate">
            <span>
              Manually switched to {activeOverride.name}. Auto-routing resumes when you switch apps.
            </span>
            <button
              type="button"
              onClick={() => send({ type: "profile.override.clear" })}
              className="font-medium underline-offset-2 hover:underline cursor-pointer"
            >
              Resume auto-routing
            </button>
          </div>
        )}

        <div className="space-y-1.5">
          <AnimatePresence initial={false}>
            {config.profiles.map((p) => {
              const isDefault = p.id === config.default_profile_id;
              const builtin = isBuiltinId(p.id);
              return (
                <ProfileRow
                  key={p.id}
                  profile={{ ...p, builtin }}
                  isDefault={isDefault}
                  targets={targetsForProfile(p.id)}
                  onOpenEditor={() => {
                    setEditorFocusName(false);
                    setEditorProfileId(p.id);
                  }}
                  reducedMotion={!!reduceMotion}
                />
              );
            })}
          </AnimatePresence>
        </div>

        {config.default_profile_id === "" && (
          <p className="text-[11px] text-steel/70 mt-2.5">
            No default profile. Apps you haven't routed get raw text.
          </p>
        )}
      {/* Profile editor MODAL (round 04, single-column) — full-window overlay
          where the name input, Default toggle, "Activated when using" chips,
          cleanup-prompt card, and "Try it" tester live. Drives both an existing
          profile (autosave + Done, `editorProfileId`) and a NEW DRAFT (buffered
          + Create, `draft`). Re-keyed so its local buffers reset per profile. */}
      {(() => {
        // The RESOLVED effective global input style for the "Default (...)"
        // parenthetical: the user's explicit pick once set, otherwise the live
        // screen-reader auto-default (keystroke when detected, else clipboard).
        const resolvedGlobalInputStyle = config.input_style_user_set
          ? config.input_style
          : config.screen_reader_detected
            ? "keystroke"
            : "clipboard";
        // Draft takes precedence: the new-profile flow opens the editor on the
        // local draft (not yet persisted). Otherwise edit the selected profile.
        if (draft) {
          const draftTargets: RoutedTarget[] = draft.rules.map((r) =>
            r.kind === "app"
              ? { kind: "app", processName: r.key, label: r.label, icon: r.icon }
              : { kind: "site", domain: r.key, label: r.label, icon: r.icon },
          );
          return (
            <ProfileEditorModal
              key={`draft-${draft.id}`}
              open
              mode="draft"
              profile={{
                id: draft.id,
                name: draft.name,
                prompt: draft.prompt,
                ...(draft.icon ? { icon: draft.icon } : {}),
                ...(draft.inputStyle ? { input_style: draft.inputStyle } : {}),
                ...(draft.language ? { language: draft.language } : {}),
                ...(draft.mode ? { mode: draft.mode } : {}),
              }}
              isDefault={!!draft.makeDefault}
              isBuiltin={false}
              promptFixedByModel={activeModelPromptFixed}
              trainedModesByModel={activeModelTrainedModes}
              fixedModelLabel={activeModelLabel}
              focusName={editorFocusName}
              testerGate={testerGate}
              targets={draftTargets}
              hotkey={draft.hotkey}
              onHotkeyChange={(keys) =>
                setDraft((d) => (d ? { ...d, hotkey: keys } : d))
              }
              globalLanguage={config.language}
              sttArtifact={resolveActiveSttArtifact(config.stt_backend, config.parakeet_model)}
              onLanguageChange={(language) => setDraft(d => d ? { ...d, language } : d)}
              globalInputStyle={resolvedGlobalInputStyle}
              onInputStyleChange={(style) =>
                setDraft((d) => (d ? { ...d, inputStyle: style } : d))
              }
              onIconChange={(icon) =>
                setDraft((d) => (d ? { ...d, icon } : d))
              }
              onModeChange={(mode) =>
                setDraft((d) => (d ? { ...d, mode } : d))
              }
              onSignatureChange={() => {
                /* Custom draft profiles do not support Email sign-offs. */
              }}
              send={send}
              subscribe={subscribe}
              connected={connected}
              onSetDefault={(next) => {
                /* Buffer the intent on the draft; commitDraft applies it to
                   default_profile_id on Create. The button label tracks this
                   via isDefault above. */
                setDraft((d) => (d ? { ...d, makeDefault: next } : d));
              }}
              onPromptChange={(prompt) =>
                setDraft((d) => (d ? { ...d, prompt } : d))
              }
              onRename={(name) => setDraft((d) => (d ? { ...d, name } : d))}
              onUnassign={(kind, key) => removeDraftRule(kind, key)}
              onAddRule={() => {
                setAddAppTargetProfileId(draft.id);
                setAddAppOpen(true);
              }}
              addRuleDisabled={!canAddPowerModeRule}
              onDelete={() => {
                /* No delete affordance in draft mode. */
              }}
              onCommit={commitDraft}
              onClose={discardDraft}
              reducedMotion={!!reduceMotion}
            />
          );
        }

        const editorProfile =
          config.profiles.find((p) => p.id === editorProfileId) ?? null;
        return (
          <ProfileEditorModal
            key={editorProfileId ?? "none"}
            open={editorProfileId !== null}
            mode="existing"
            profile={editorProfile}
            isDefault={editorProfile?.id === config.default_profile_id}
            isBuiltin={editorProfile ? isBuiltinId(editorProfile.id) : false}
            promptFixedByModel={activeModelPromptFixed}
            trainedModesByModel={activeModelTrainedModes}
            fixedModelLabel={activeModelLabel}
            onChangeModel={() => {
              setEditorProfileId(null);
              onChangeModel();
            }}
            focusName={editorFocusName}
            testerGate={testerGate}
            targets={editorProfile ? targetsForProfile(editorProfile.id) : []}
            hotkey={
              editorProfile ? (config.profile_hotkeys?.[editorProfile.id] ?? "") : ""
            }
            onHotkeyChange={(keys) => {
              if (editorProfile) setProfileHotkey(editorProfile.id, keys);
            }}
            globalLanguage={config.language}
            sttArtifact={resolveActiveSttArtifact(config.stt_backend, config.parakeet_model)}
            onLanguageChange={(language) => {
              if (!editorProfile) return;
              onConfigChange({
                profiles: config.profiles.map((p) => {
                  if (p.id !== editorProfile.id) return p;
                  const { language: _previous, ...rest } = p;
                  return language ? { ...rest, language } : rest;
                }),
              });
            }}
            globalInputStyle={resolvedGlobalInputStyle}
            onInputStyleChange={(style) => {
              if (editorProfile) setProfileInputStyle(editorProfile.id, style);
            }}
            onIconChange={(icon) => {
              if (editorProfile) setProfileIcon(editorProfile.id, icon);
            }}
            onModeChange={(mode) => {
              if (editorProfile) setProfileMode(editorProfile.id, mode);
            }}
            onSignatureChange={(signature) => {
              if (editorProfile) setProfileSignature(editorProfile.id, signature);
            }}
            send={send}
            subscribe={subscribe}
            connected={connected}
            onSetDefault={(next) => {
              if (editorProfile) setProfileDefault(editorProfile.id, next);
            }}
            onPromptChange={(prompt) => {
              if (editorProfile) setProfilePrompt(editorProfile.id, prompt);
            }}
            onRename={(name) => {
              if (editorProfile) renameProfile(editorProfile.id, name);
            }}
            onUnassign={(kind, key) => {
              if (kind === "app") unassignApp(key);
              else unassignSite(key);
            }}
            onAddRule={() => {
              if (!editorProfile) return;
              setAddAppTargetProfileId(editorProfile.id);
              setAddAppOpen(true);
            }}
            addRuleDisabled={!canAddPowerModeRule}
            onDelete={() => {
              if (editorProfile) removeProfile(editorProfile.id);
            }}
            onCommit={() => {
              /* existing profiles autosave; Create is draft-only. */
            }}
            onClose={() => setEditorProfileId(null)}
            reducedMotion={!!reduceMotion}
          />
        );
      })()}

      {/* Add-app MODAL (round 03/04) — full-window overlay: pick a running app
          and choose which profile cleans its dictation. Reuses RunningAppList.
          When opened from a DRAFT, the draft profile is the only chooser option
          and the pick is written to the draft's local pending rules (so Cancel
          discards them); for an existing profile it writes through to config. */}
      <AddAppModal
        open={addAppOpen}
        profiles={
          draft ? [{ id: draft.id, name: draft.name || "New profile", prompt: draft.prompt }] : config.profiles
        }
        initialProfileId={
          draft ? draft.id : (addAppTargetProfileId ?? config.default_profile_id)
        }
        runningApps={runningApps}
        installedApps={installedApps}
        appCapability={appIdentityCapability}
        websiteCapability={browserIdentityCapability}
        pathBrowseCapability={appPathBrowseCapability}
        loading={runningLoading}
        mappedProcessNames={(() => {
          // Hide ONLY apps already routed to the TARGET profile (apps routed to
          // OTHER profiles stay in the list so they can be moved here, gated by
          // the modal's move confirmation). The modal unions its own pending app
          // keys on top.
          if (draft) {
            // Draft target: its own staged app rules are the "already routed
            // here" set; nothing is in power_mode_map for the draft yet.
            return new Set(
              draft.rules.filter((r) => r.kind === "app").map((r) => r.key.toLowerCase()),
            );
          }
          const targetId = addAppTargetProfileId ?? config.default_profile_id;
          return new Set(
            Object.entries(powerModeMap)
              .filter(([, pid]) => pid === targetId)
              .map(([k]) => k.toLowerCase()),
          );
        })()}
        otherProfileName={(kind, key) => {
          // The name of the profile a key is routed to, when that's NOT the
          // target (so the modal can confirm a move). Draft keys are never in
          // the maps under the draft id, so a key mapped anywhere is "other".
          const targetId = draft ? draft.id : (addAppTargetProfileId ?? config.default_profile_id);
          const map = kind === "app" ? powerModeMap : siteMap;
          const lower = key.toLowerCase();
          const pid = map[lower] ?? map[key];
          if (!pid || pid === targetId) return null;
          return config.profiles.find((p) => p.id === pid)?.name ?? null;
        }}
        onAdd={(rules, profileId) => {
          if (draft && profileId === draft.id) {
            // Draft target: functional setDraft is safe to call repeatedly.
            rules.forEach((r) => addDraftRule(r.kind, r.key, r.path ?? null));
          } else {
            // Existing profile: merge ALL app keys into power_mode_map and ALL
            // site keys into site_map in ONE config write. Calling assignApp /
            // assignSite in a loop would snapshot the maps and clobber each other,
            // keeping only the last key — so build both next maps locally here.
            // App rules also record their exe path into power_mode_paths so
            // install state can later be checked by path existence.
            const nextPowerMap = { ...powerModeMap };
            const nextSiteMap = { ...siteMap };
            const nextPaths = { ...(config.power_mode_paths ?? {}) };
            for (const r of rules) {
              if (r.kind === "app") {
                nextPowerMap[r.key.toLowerCase()] = profileId;
                if (r.path) nextPaths[r.key.toLowerCase()] = r.path;
              } else {
                nextSiteMap[normalizeDomain(r.key) ?? r.key.toLowerCase()] = profileId;
              }
            }
            onConfigChange({
              power_mode_map: nextPowerMap,
              site_map: nextSiteMap,
              power_mode_paths: nextPaths,
            });
          }
        }}
        onClose={() => setAddAppOpen(false)}
      />
    </>
  );
}
