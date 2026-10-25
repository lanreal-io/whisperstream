import type { ComponentType } from "react";
import * as edition from "@edition/updater";
import type { DailyUseCapabilityReport } from "./dailyUseCapabilities";

/** A checked update the official updater asks the app to present. */
export interface OfficialUpdateOffer {
  version: string;
  body?: string;
}

export interface AboutUpdateCheckProps {
  capabilities?: DailyUseCapabilityReport;
  /** Current Lockdown state; null while unknown. */
  lockdown: boolean | null;
  onUpdateAvailable?: (update: OfficialUpdateOffer) => Promise<void>;
}

/**
 * The official installer's updater. A build that ships one points the
 * `@edition/updater` alias at its provider (see frontend/edition.ts);
 * otherwise the alias resolves to officialUpdaterStub.ts and every consumer
 * treats null as "no updates, no update UI". The provider's popup window root
 * loads lazily through loadOfficialUpdatePopup.
 */
export interface OfficialUpdater {
  /** Starts scheduled background checks; returns the cleanup. */
  startAutoCheck: (show: (update: OfficialUpdateOffer) => Promise<void>) => () => void;
  AboutUpdateCheck: ComponentType<AboutUpdateCheckProps>;
}

/** The contract every `@edition/updater` module, the stub included, exports. */
export interface OfficialUpdaterModule {
  officialUpdater: OfficialUpdater | null;
  loadOfficialUpdatePopup: (() => Promise<ComponentType>) | null;
}

const provider: OfficialUpdaterModule = edition;

export const officialUpdater: OfficialUpdater | null = provider.officialUpdater;

/** Loads the provider's update popup root; null without a provider. */
export const loadOfficialUpdatePopup: (() => Promise<ComponentType>) | null =
  provider.loadOfficialUpdatePopup;
