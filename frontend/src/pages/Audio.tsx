import { useState, useEffect } from "react";
import { SettingsSection as Section } from "../components/SettingsSection";
import { Toggle } from "../components/Toggle";
import { ModeSelector } from "../components/ModeSelector";
import { Dropdown, type DropdownOption } from "../components/Dropdown";
import { getDevices, type AudioDevice } from "../lib/api/system";
import { type AppConfig } from "../lib/api/config";
import {
  inputDevicePatch,
  inputDeviceValue,
  savedInputDeviceAvailability,
  savedInputDeviceLabel,
  selectedInputDevice,
  usesOpaqueInputIdentity,
  type InputDeviceInventoryStatus,
} from "../lib/inputDeviceSelection";
import {
  capabilityDecision,
  capabilityExplanation,
  type DailyUseCapabilityReport,
} from "../lib/dailyUseCapabilities";
import { RangeInput } from "../components/RangeInput";
import { SliderField, SliderValue } from "../components/SliderField";

interface AudioProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  capabilities: DailyUseCapabilityReport | null;
}

export function Audio({ config, onConfigChange, capabilities }: AudioProps) {
  const [devices, setDevices] = useState<AudioDevice[] | null>(null);
  const [inventoryStatus, setInventoryStatus] =
    useState<InputDeviceInventoryStatus>("loading");
  const audioPolicy = capabilityDecision(capabilities, "audio-policy");
  const selectionMode = config.audio_input_device_selection_mode;
  const usesOpaqueIdentity = usesOpaqueInputIdentity(selectionMode);
  const inventory = devices ?? [];

  useEffect(() => {
    getDevices()
      .then((nextDevices) => {
        setDevices(nextDevices);
        setInventoryStatus("loaded");
      })
      .catch(() => {
        setDevices(null);
        setInventoryStatus("error");
      });
  }, []);

  const deviceOptions: DropdownOption<string>[] = [
    { label: "Use system default", value: "" },
    ...inventory.map((device) => ({
      label: device.name,
      value: inputDeviceValue(device, selectionMode),
    })),
  ];

  const currentDevice = selectedInputDevice(
    config.audio_input_device,
    inventory,
    selectionMode,
  );

  // Preserve a saved device that is no longer enumerated (e.g. unplugged) so
  // the selection still shows instead of silently snapping to default.
  if (
    config.audio_input_device &&
    !currentDevice
  ) {
    const availability = savedInputDeviceAvailability(
      config.audio_input_device,
      currentDevice,
      selectionMode,
      inventoryStatus,
      config.audio_input_device_available,
    );
    deviceOptions.push({
      label: savedInputDeviceLabel(config, currentDevice, selectionMode),
      value: config.audio_input_device,
      description:
        availability === "unavailable"
          ? "Unavailable"
          : availability === "unknown"
            ? "Availability unknown"
            : undefined,
      disabled: usesOpaqueIdentity && availability !== "available",
    });
  }

  return (
    <div>
      {/* Input Device */}
      <Section inline index={0} title="Input Device" description="Choose which microphone to use">
        <Dropdown
          value={config.audio_input_device}
          options={deviceOptions}
          onChange={(value) => {
            const device = inventory.find(
              (candidate) => inputDeviceValue(candidate, selectionMode) === value,
            );
            onConfigChange(inputDevicePatch(device ?? null, selectionMode));
          }}
          ariaLabel="Input device"
          triggerClassName="w-[240px]"
          panelWidth={300}
          placeholder="Use system default"
        />
      </Section>

      {/* Recording Chime */}
      <Section inline index={1} title="Recording Chime" description="Play a chime when recording starts and stops">
        <Toggle
          checked={config.audio_feedback}
          onChange={(v) => onConfigChange({ audio_feedback: v })}
        />
      </Section>

      {/* Chime Volume */}
      <Section inline index={2} title="Chime Volume">
        <SliderField className="w-[240px]" value={<SliderValue>{Math.round(config.audio_feedback_volume * 100)}%</SliderValue>}>
          <RangeInput
            ariaLabel="Chime volume"
            min={0}
            max={1}
            step={0.05}
            value={config.audio_feedback_volume}
            onChange={(audio_feedback_volume) => onConfigChange({ audio_feedback_volume })}
            className=""
          />
        </SliderField>
      </Section>

      {/* Audio During Recording */}
      <Section
        inline
        last
        index={3}
        title="Audio While Recording"
        description={
          audioPolicy.enabled
            ? "Lower or mute other audio while recording"
            : capabilityExplanation(audioPolicy)
        }
      >
        <ModeSelector
          ariaLabel="Audio while recording"
          value={config.system_volume_mode}
          onChange={(system_volume_mode) => onConfigChange({ system_volume_mode })}
          options={[
            { value: "off", label: "None" },
            {
              value: "reduce",
              label: "Lower",
              disabled: !audioPolicy.enabled,
              hint: !audioPolicy.enabled ? capabilityExplanation(audioPolicy) : undefined,
            },
            {
              value: "mute",
              label: "Mute",
              disabled: !audioPolicy.enabled,
              hint: !audioPolicy.enabled ? capabilityExplanation(audioPolicy) : undefined,
            },
          ]}
        />
      </Section>
    </div>
  );
}
