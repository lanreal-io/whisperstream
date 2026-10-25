# App basics and updates

Whisperstream 2.0.0.

## Keep Whisperstream available

Whisperstream stays available from the Windows notification area, or the menu bar on a Mac, while you work. Closing the main window hides it instead of ending the app, so your dictation shortcuts continue to work.

| Action | Result |
|---|---|
| Close the Whisperstream window | Hides the window and keeps Whisperstream running. |
| Select the Whisperstream tray icon | Opens the window, or brings it to the front. |
| Select **Quit** from the tray menu | Stops Whisperstream and its dictation shortcuts. |

After selecting **Quit**, use the Start menu or desktop shortcut to start an installed app again. For a source checkout, restart its launcher.

On a Mac, the Whisperstream icon is in the menu bar. Click it to open the window, or open its menu for **Settings** and **Quit**. The Dock icon appears only while a Whisperstream window is open. To start Whisperstream again after quitting, open it from Applications.

Turn on **Home > Run on Startup** to start Whisperstream in the background when you sign in. On a Mac, macOS may ask you to approve Whisperstream in System Settings before startup is enabled. On the same page, choose **Light** or **Dark** to change Whisperstream's appearance.

## Included features and updates

All features are included free. No license key or activation server is required.

How you update depends on how you installed Whisperstream:

- **Official installer (Windows and Mac):** Whisperstream checks for a signed update at most about
  once a day, and when you select **Check for updates** in **About**. When an
  update is available, select **Update now** to download and install it. Nothing
  is downloaded or installed until you accept, and Whisperstream verifies the
  update's signature first. Lockdown Mode stops update checks.
- **Built from source:** Whisperstream makes no update checks or downloads. To
  update, get newer source from the [source repository](https://github.com/lanreal-io/whisperstream) and follow
  its README to rebuild. **About > Source code** opens the repository.

To switch from the older commercial edition, run the open-source installer.

Updates and responses to reports are provided as available; no schedule or
guaranteed support is promised.

Keep your installer, or your source revision and its build instructions, and a
backup of the models you use. See [Install and recover models offline](model-recovery.md).

## Get help or report a bug

Open **About** to use these actions:

- **Report a bug** opens a public bug-report form on GitHub.
- **Request a feature** opens a public feature-request form on GitHub.
- **Read documentation** opens the online guide.
- **Source code** opens the Whisperstream repository on GitHub.

GitHub issues are public. Do not include transcripts, recordings, API keys, or
personal information. For private support or security reports, email
[support@whisperstream.io](mailto:support@whisperstream.io).

No report is submitted by opening a link. Export logs locally and review them
before attaching. Attaching a file uploads it to GitHub immediately, before
submitting the issue. Nothing is uploaded automatically.

External support links and update checks are unavailable in Lockdown Mode.
Local documentation, legal documents, and log exports remain available.

## Save the documentation

Open **About** and select **Save documentation**. Choose a folder in the folder picker. Whisperstream saves a PDF of the complete guide and an unpacked Markdown folder for search and AI tools. Both formats are bundled with the app and work without the website.

When **Documentation saved** appears, select **Open folder** to find the files. Cancelling the folder picker does not save anything. Repeated saves use a separate folder so existing files are preserved.

For local diagnostic information, see [Export logs](settings-data-locations.md#export-logs).

## Uninstall Whisperstream or remove a source checkout

For a source development checkout, stop its launcher before removing the checkout.
Keep or remove its isolated data separately; see the source README.

For an installed app, on Windows 11 open **Settings > Apps > Installed apps**. On Windows 10, open **Settings > Apps > Apps & features**. Find Whisperstream and select **Uninstall**.

The uninstaller asks whether to remove Whisperstream's local data with the app. Review [what survives uninstall](settings-data-locations.md#choose-what-survives-uninstall) before choosing.

On a Mac, select **Quit** from the menu bar icon, then move Whisperstream from Applications to the Trash. This leaves your settings, history, and models in place; see [what survives uninstall](settings-data-locations.md#choose-what-survives-uninstall) to remove them.
