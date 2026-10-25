import type { OfficialUpdaterModule } from "./officialUpdater";

// Default target of the `@edition/updater` alias: no updater, no update UI.
export const officialUpdater: OfficialUpdaterModule["officialUpdater"] = null;
export const loadOfficialUpdatePopup: OfficialUpdaterModule["loadOfficialUpdatePopup"] = null;
