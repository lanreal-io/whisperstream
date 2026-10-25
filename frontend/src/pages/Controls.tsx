import { useState } from "react";
import { SettingsSection as Section } from "../components/SettingsSection";
import { ShortcutCapture } from "../components/ShortcutCapture";
import { Toggle } from "../components/Toggle";
import { ModeSelector } from "../components/ModeSelector";
import { AdvancedDisclosure } from "../components/AdvancedDisclosure";
import { SettingsGroup } from "../components/SettingsGroup";
import { TypingDelaySelect } from "../components/TypingDelaySelect";
import type { AppConfig } from "../lib/api/config";
import { currentShortcutConflict, crossFieldConflict } from "../lib/shortcutConflict";
import type { AppEvent } from "../lib/socket/events";

interface ControlsProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
  /** Surface a transient status message in the Sidebar toast. */
  onShowStatus: (message: string, isError: boolean) => void;
}

export function Controls({
  config,
  onConfigChange,
  send,
  subscribe,
  connected,
}: ControlsProps) {
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // The DISPLAYED active style is the resolved value: the user's explicit pick
  // once set, otherwise the live screen-reader auto-default (keystroke when a
  // screen reader is detected, else clipboard). Drives the pills + captions.
  const effectiveStyle = config.input_style_user_set
    ? config.input_style
    : config.screen_reader_detected
      ? "keystroke"
      : "clipboard";
  // Show the auto-default hint only while the user hasn't picked AND a screen
  // reader is forcing keystroke. When shown, it precedes/replaces the keystroke
  // reassurance line (it explains the why).
  const showScreenReaderHint =
    !config.input_style_user_set && !!config.screen_reader_detected;

  return (
    <div>
      {/* Group 1: Recording Indicator. The heading names the feature, so each
          row only has to name its own switch (design.md D4). Rows inside a group
          are joined by whitespace, never hairlines (D3). */}
      <SettingsGroup first title="Recording Indicator" entranceDelay={0}>
        <Section dimmed={false}
          inline
          last
          index={1}
          title="Show indicator"
          description="Display an overlay while you dictate"
        >
          <Toggle
            checked={config.show_recording_indicator}
            onChange={(v) => onConfigChange({ show_recording_indicator: v })}
            ariaLabel="Show indicator"
          />
        </Section>

        <Section
          inline
          last
          index={2}
          title="Keep control visible"
          description="Show a compact control on your desktop between recordings"
          dimmed={!config.show_recording_indicator}
        >
          <Toggle
            checked={config.show_recording_control}
            onChange={(v) =>
              onConfigChange({ show_recording_control: v })
            }
            disabled={!config.show_recording_indicator}
            ariaLabel="Keep control visible"
          />
        </Section>

        {/* Live Preview is persisted independently but Recording Indicator is
            the runtime and interaction master gate. */}
        <Section
          inline
          last
          index={3}
          title="Live Preview"
          description="Preview raw dictation in the overlay as you speak"
          dimmed={!config.show_recording_indicator}
        >
          <Toggle
            checked={config.show_live_preview}
            onChange={(v) => onConfigChange({ show_live_preview: v })}
            disabled={!config.show_recording_indicator}
            ariaLabel="Live Preview"
          />
        </Section>
      </SettingsGroup>

      {/* Group 2: Hotkeys. Conflict captions stay directly under their row,
          inside the group. */}
      <SettingsGroup title="Hotkeys" entranceDelay={3 * 0.03}>
      {/* Push to Talk */}
      <Section dimmed={false} inline last index={4} title="Push to Talk" description="Hold this key to record, release to stop">
        <ShortcutCapture
          value={config.push_to_talk_shortcut}
          onChange={(keys) => onConfigChange({ push_to_talk_shortcut: keys })}
          send={send}
          subscribe={subscribe}
          connected={connected}
          captureTarget="push_to_talk"
          clearAriaLabel="Clear Push to Talk shortcut"
        />
      </Section>
      {(currentShortcutConflict(config.push_to_talk_shortcut) || crossFieldConflict(config.push_to_talk_shortcut, [config.toggle_shortcut, config.paste_last_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])) && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          {currentShortcutConflict(config.push_to_talk_shortcut) || crossFieldConflict(config.push_to_talk_shortcut, [config.toggle_shortcut, config.paste_last_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])}
        </div>
      )}

      {/* Toggle */}
      <Section dimmed={false} inline last index={5} title="Toggle" description="Press to start, press again to stop">
        <ShortcutCapture
          value={config.toggle_shortcut}
          onChange={(keys) => onConfigChange({ toggle_shortcut: keys })}
          send={send}
          subscribe={subscribe}
          connected={connected}
          captureTarget="toggle"
          clearAriaLabel="Clear Toggle shortcut"
        />
      </Section>
      {(currentShortcutConflict(config.toggle_shortcut) || crossFieldConflict(config.toggle_shortcut, [config.push_to_talk_shortcut, config.paste_last_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])) && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          {currentShortcutConflict(config.toggle_shortcut) || crossFieldConflict(config.toggle_shortcut, [config.push_to_talk_shortcut, config.paste_last_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])}
        </div>
      )}
      {!config.push_to_talk_shortcut && !config.toggle_shortcut && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          No recording shortcut is set. Set Push to Talk or Toggle to start dictating.
        </div>
      )}

      {/* Paste Last Transcript */}
      <Section dimmed={false} inline last index={6} title="Paste Last Transcript" description="Paste the most recent transcription">
        <ShortcutCapture
          value={config.paste_last_shortcut}
          onChange={(keys) => onConfigChange({ paste_last_shortcut: keys })}
          send={send}
          subscribe={subscribe}
          connected={connected}
          captureTarget="paste_last"
          clearAriaLabel="Clear Paste Last Transcript shortcut"
        />
      </Section>
      {(currentShortcutConflict(config.paste_last_shortcut) || crossFieldConflict(config.paste_last_shortcut, [config.push_to_talk_shortcut, config.toggle_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])) && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          {currentShortcutConflict(config.paste_last_shortcut) || crossFieldConflict(config.paste_last_shortcut, [config.push_to_talk_shortcut, config.toggle_shortcut, config.paste_last_original_shortcut, config.claude_view_shortcut ?? ""])}
        </div>
      )}

      {/* Paste Last Transcript (Original) */}
      <Section dimmed={false} inline last index={7} title="Paste Last Transcript (Original)" description="Paste the most recent transcription before enhancement">
        <ShortcutCapture
          value={config.paste_last_original_shortcut}
          onChange={(keys) => onConfigChange({ paste_last_original_shortcut: keys })}
          send={send}
          subscribe={subscribe}
          connected={connected}
          captureTarget="paste_last_original"
          clearAriaLabel="Clear Paste Last Transcript (Original) shortcut"
        />
      </Section>
      {(currentShortcutConflict(config.paste_last_original_shortcut) || crossFieldConflict(config.paste_last_original_shortcut, [config.push_to_talk_shortcut, config.toggle_shortcut, config.paste_last_shortcut, config.claude_view_shortcut ?? ""])) && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          {currentShortcutConflict(config.paste_last_original_shortcut) || crossFieldConflict(config.paste_last_original_shortcut, [config.push_to_talk_shortcut, config.toggle_shortcut, config.paste_last_shortcut, config.claude_view_shortcut ?? ""])}
        </div>
      )}

      </SettingsGroup>

      {/* Input Style and Advanced stay ungrouped: a heading is earned by two or
          more visible rows, and Typing Delay is hidden behind the disclosure.
          The boundary has no heading to mark it, so it keeps a hairline (D3
          rule 2). */}
      <div className="h-px my-3" style={{ background: "var(--divider)" }} />

      {/* Input Style */}
      <Section dimmed={false} inline last index={8} title="Input Style" description="How transcribed text reaches the focused window">
        <ModeSelector<"clipboard" | "keystroke">
          ariaLabel="Input style"
          value={effectiveStyle as "clipboard" | "keystroke"}
          onChange={(input_style) =>
            onConfigChange({ input_style, input_style_user_set: true })
          }
          options={[
            { value: "clipboard", label: "Clipboard" },
            {
              value: "keystroke",
              label: "Keystroke",
              // Keystroke is the non-obvious choice, so gloss it on hover.
              // Clipboard is the familiar default and needs no explanation.
              hint: "Can help when pasting is blocked.",
            },
          ]}
        />
      </Section>

      {/* Screen-reader auto-default hint — shown only when the user hasn't picked
          a style and a screen reader has auto-selected keystroke. It explains the
          why; with no screen reader detected, the picker shows no caption. */}
      {showScreenReaderHint && (
        <div className="text-[11px] text-steel -mt-2 pb-3">
          Keystroke is on automatically because a screen reader is detected. Pick a style to override.
        </div>
      )}

      {/* Advanced disclosure — Typing Delay (global). Kept last so it closes the page. */}
      <AdvancedDisclosure
        open={advancedOpen}
        onToggle={() => setAdvancedOpen((v) => !v)}
        entranceDelay={9 * 0.03}
      >
        <div className="mt-3">
          <Section dimmed={false}
            inline
            last
            index={0}
            title="Typing Delay"
            description="Pause between keystrokes for slow remote sessions that drop fast typing"
          >
            <TypingDelaySelect
              value={config.keystroke_delay_ms}
              onChange={(ms) => onConfigChange({ keystroke_delay_ms: ms })}
              ariaLabel="Typing delay"
              disabled={effectiveStyle !== "keystroke"}
            />
          </Section>
        </div>
      </AdvancedDisclosure>
    </div>
  );
}
