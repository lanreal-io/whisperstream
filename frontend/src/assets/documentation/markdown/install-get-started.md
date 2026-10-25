# Install and configure Whisperstream

Whisperstream 2.0.0.

## Install Whisperstream

Whisperstream supports Windows 10 and Windows 11 on x64 computers. A Mac version
is available as a beta for Macs with Apple silicon running macOS 14 Sonoma or
later. Download the free official installer from [Whisperstream](https://whisperstream.io).

### Windows

1. Download and open the Whisperstream installer.
2. Review the license agreement and select **I Agree** to continue.
3. Choose whether to install for your Windows account or make the app available across Windows accounts.
4. Approve elevation if you selected an all-users installation.
5. Launch Whisperstream when setup finishes.

Choose **Install just for me** for your Windows account. Choose **Install for anyone using this computer** to make the app available to other Windows accounts when you can approve the administrator prompt. Each user still accepts the EULA.

If you are switching from an earlier installation, back up your data and keep its installer before installing the open-source edition. The switch requires running the new installer yourself.

### Mac (beta)

1. Download and open the Whisperstream disk image (DMG).
2. Drag Whisperstream to your Applications folder.
3. Open Whisperstream from Applications.
4. Review the license agreement and select **I agree** to continue.

Intel Macs are not supported.

## Build and run from source

Start with the [source README](https://github.com/lanreal-io/whisperstream#readme)
for requirements, exact dependencies and the contributor launcher. Run the native
Tauri shell for dictation into other applications. Browser mode is for UI
development and does not prove native text insertion or window behavior.
The source uses the public sherpa-onnx and sherpa-onnx-core 1.13.8 packages. No
private package archive or official signing credentials are required.

The launcher keeps source development data separate from existing installations.
Follow the selected revision's setup and update instructions.

## Complete first-run setup

On first launch, Whisperstream asks you to accept the EULA before the main setup opens. The guided setup then helps you:

1. Choose a dictation language.
2. On a Mac, allow Microphone and Accessibility access. See [Allow access on a Mac](install-get-started.md#allow-access-on-a-mac).
3. Confirm or change the Push to Talk shortcut while the matching speech model downloads in the background.
4. Try dictation in the setup text box after the model is ready.
5. Optionally, turn on AI enhancement with Stream-1 Mini, Whisperstream's on-device cleanup model.

Follow the progress shown in setup. Dictation becomes available when the model is ready.

The enhancement step is optional. Choose **Skip & finish** to leave enhancement off and download nothing. Choose **Turn on enhancement** to download Stream-1 Mini (about 460 MB) and check how fast it runs on this computer:

- If it runs well, local enhancement turns on and you can try two examples before finishing.
- If it runs slowly, choose **Use it anyway** or **Leave enhancement off**.
- If it is too slow, enhancement stays off.

You can change this later in **Enhancement**; see [Enhancement providers](enhancement-providers.md). To prepare an offline installation or restore model files manually, see [Install and recover models offline](model-recovery.md).

## Allow access on a Mac

Whisperstream needs two macOS permissions:

| Permission | Why it is needed |
|---|---|
| **Microphone** | Records your voice for dictation. |
| **Accessibility** | Lets your shortcut work in other apps and types text into them. |

In setup, select **Allow microphone** and **Allow accessibility**, then respond to the macOS prompts. If access was denied earlier, select **Open System Settings**, go to **Privacy & Security**, open **Microphone** or **Accessibility**, turn on Whisperstream, then return and select **Check again**. If Accessibility is on but Whisperstream still asks for it, select **Restart app**.

Without Accessibility, your shortcuts and text insertion do not work. To check access later, open **Home** and find **Permissions**.

## Make your first dictation

The default Push to Talk shortcut is Right Shift.

1. Use the test step in setup, or finish setup and click a text field in another app.
2. Hold your Push to Talk shortcut (Right Shift if you did not change it).
3. Speak one short sentence in the language you selected.
4. Release the shortcut to stop recording.
5. Wait for Whisperstream to transcribe the sentence. The text appears in the setup box or the selected app.

If the setup test works, installation is complete.

## Choose your next guide

- Change recording shortcuts or reuse the last dictation: [Recording controls](recording-controls.md)
- Change language or review speech models: [Languages and models](languages-models.md)
- Choose a microphone or adjust recording sounds: [Audio](audio.md)
- Fix delivery in a particular application: [Text insertion](text-insertion.md)
- Learn how updates and uninstall work: [App basics](app-basics.md)

Use the documentation navigation for every other feature.
