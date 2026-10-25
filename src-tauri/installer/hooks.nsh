; ---------------------------------------------------------------------------
; Whisperstream NSIS installer hooks.
;
; LangString overrides for the "app is running" prompt and the uninstaller
; "Delete app data" checkbox live in installer/whisperstream-en.nsh, wired
; up via tauri.conf.json's bundle.windows.nsis.customLanguageFiles.
; That file is loaded by Tauri's NSIS template AFTER its default English.nsh,
; so its LangString redefinitions win. Hooks-file LangString edits do NOT
; win because hooks.nsh is included BEFORE English.nsh.
;
; The auto-generated installer template (see
; src-tauri/target/release/nsis/x64/installer.nsi) wires the "Delete app data"
; checkbox to:
;
;     RmDir /r "$APPDATA\${BUNDLEID}"
;     RmDir /r "$LOCALAPPDATA\${BUNDLEID}"
;
; where BUNDLEID = "com.whisperstream.app". The Python backend, however,
; writes config, logs, models, license.json, acceptance.json, etc. to
; %LOCALAPPDATA%\Whisperstream (no com. prefix; see
; src/whisperstream/config_manager.py::get_app_dir()). Without this hook the
; checkbox would do nothing useful.
;
; This POSTUNINSTALL hook fires after the template's own delete-app-data
; block. When the user checked the box (and we are not in updater mode) we
; remove the real data directory.
;
; GRACEFUL CLOSE MACRO
; --------------------
; WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL encapsulates the full process-cleanup
; sequence used by both NSIS_HOOK_PREINSTALL and NSIS_HOOK_PREUNINSTALL:
;
;   1. Skip when $UpdateMode = 1 (in-app updater handles its own shutdown).
;   2. Check whether Whisperstream.exe is running via
;      nsis_tauri_utils::FindProcess (returns 0 in $R0 when found; same
;      calling convention as CheckIfAppIsRunning in utils.nsh lines 22-71).
;      If not running, no-op.
;   3. Send graceful quit: nsExec::ExecToLog '"$INSTDIR\Whisperstream.exe" --quit'
;      The tauri-plugin-single-instance callback fires on the running instance,
;      calls app.exit(0), which in turn fires the RunEvent::Exit handler that
;      kills the engine process tree.
;   4. Poll up to ~5 s (10 x Sleep 500) for BOTH Whisperstream.exe AND
;      whisperstream-engine.exe to disappear. Loop exits early when both are
;      gone. This is required because app.exit(0) returns before the engine
;      process has fully exited (race window documented in research.md).
;   5. Force-kill fallback: if either process is still alive after the poll,
;      call nsis_tauri_utils::KillProcess for each. As an additional belt-and-
;      suspenders fallback, also run taskkill /F /T /IM for both, which handles
;      the case where KillProcess is not able to terminate the process.
;
; Scratch registers used: $R0, $R4, $R5, $R6.
; $R4 = shell-found flag, $R5 = engine-found flag, $R6 = loop counter.

!include "FileFunc.nsh"

; MANAGED LOCKDOWN POLICY
; -----------------------
; /DISABLEHISTORY=1 independently disables transcript and audio history.
; Omitted switches preserve their respective customer-owned values.
; /MANAGEDLOCKDOWN=1 is an opt-in deployment switch. It writes the same
; machine policy that EAS, Intune, and Group Policy can manage independently:
;
;   HKLM\SOFTWARE\Policies\Whisperstream\ManagedLockdown = 1 (DWORD)
;
; The registry view is always explicit because the Python runtime reads the
; 64-bit policy view. A failed or unverifiable HKLM write terminates the
; installer with ERROR_ELEVATION_REQUIRED (740); there is deliberately no
; per-user fallback. The policy is customer-owned configuration, so neither
; update nor uninstall code removes it.

!macro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE MESSAGE ERRORCODE
  DetailPrint "Managed Lockdown policy error: ${MESSAGE}"
  MessageBox MB_ICONSTOP|MB_OK \
    "Whisperstream could not apply Managed Lockdown.$\r$\n$\r$\n${MESSAGE}" \
    /SD IDOK
  SetErrorLevel ${ERRORCODE}
  Quit
!macroend

!macro WHISPERSTREAM_APPLY_MANAGED_LOCKDOWN_POLICY
  ${GetParameters} $R0
  ; Validate both supplied values before changing either policy.
  ClearErrors
  ${GetOptions} $R0 "/MANAGEDLOCKDOWN=" $R1
  ${IfNot} ${Errors}
    ${If} $R1 != "1"
      !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
        "The /MANAGEDLOCKDOWN option accepts only the value 1." \
        87
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/DISABLEHISTORY=" $R1
  ${IfNot} ${Errors}
    ${If} $R1 != "1"
      !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
        "The /DISABLEHISTORY option accepts only the value 1." \
        87
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/MANAGEDLOCKDOWN=" $R1
  ${IfNot} ${Errors}
    ${If} $R1 == "1"
      DetailPrint "Applying 64-bit machine Managed Lockdown policy"
      SetRegView 64
      ClearErrors
      WriteRegDWORD HKLM "SOFTWARE\Policies\Whisperstream" "ManagedLockdown" 1
      ${If} ${Errors}
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "Run the installer elevated as an administrator or in machine/SYSTEM context." \
          740
      ${EndIf}

      ClearErrors
      ReadRegDWORD $R3 HKLM "SOFTWARE\Policies\Whisperstream" "ManagedLockdown"
      ${If} ${Errors}
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "The 64-bit machine policy could not be verified after it was written." \
          740
      ${ElseIf} $R3 != 1
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "The 64-bit machine policy did not retain the required value." \
          740
      ${EndIf}
    ${Else}
      !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
        "The /MANAGEDLOCKDOWN option accepts only the value 1." \
        87
    ${EndIf}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/DISABLEHISTORY=" $R1
  ${IfNot} ${Errors}
    ${If} $R1 == "1"
      DetailPrint "Applying 64-bit machine history policy"
      SetRegView 64
      ClearErrors
      WriteRegDWORD HKLM "SOFTWARE\Policies\Whisperstream" "DisableHistory" 1
      ${If} ${Errors}
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "Run the installer elevated as an administrator or in machine/SYSTEM context." \
          740
      ${EndIf}

      ClearErrors
      ReadRegDWORD $R3 HKLM "SOFTWARE\Policies\Whisperstream" "DisableHistory"
      ${If} ${Errors}
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "The 64-bit machine policy could not be verified after it was written." \
          740
      ${ElseIf} $R3 != 1
        !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
          "The 64-bit machine policy did not retain the required value." \
          740
      ${EndIf}
    ${Else}
      !insertmacro WHISPERSTREAM_MANAGED_LOCKDOWN_FAILURE \
        "The /DISABLEHISTORY option accepts only the value 1." \
        87
    ${EndIf}
  ${EndIf}
!macroend

!macro WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL
  ${If} $UpdateMode <> 1
    ; --- Step 1: check if the shell is running at all ---
    nsis_tauri_utils::FindProcess "Whisperstream.exe"
    Pop $R0
    ${If} $R0 = 0
      ; Shell is running. Send graceful --quit via the single-instance channel.
      ; The tauri-plugin-single-instance callback on the running instance
      ; calls app.exit(0), which fires RunEvent::Exit to kill the engine tree.
      nsExec::ExecToLog '"$INSTDIR\Whisperstream.exe" --quit'

      ; --- Step 2: poll for both processes to exit (up to ~5 s) ---
      ; $R4 = FindProcess result for shell (0 = still running)
      ; $R5 = FindProcess result for engine (0 = still running)
      ; $R6 = iteration counter (max 10 = ~5 s total)
      ; app.exit(0) returns before the engine fully exits (race window), so
      ; we MUST poll for whisperstream-engine.exe, not just Whisperstream.exe.
      StrCpy $R4 0
      StrCpy $R5 0
      StrCpy $R6 0
      ${Do}
        Sleep 500
        IntOp $R6 $R6 + 1

        nsis_tauri_utils::FindProcess "Whisperstream.exe"
        Pop $R4
        nsis_tauri_utils::FindProcess "whisperstream-engine.exe"
        Pop $R5

        ; Both gone (non-zero = not found) — exit poll early.
        ${If} $R4 <> 0
        ${AndIf} $R5 <> 0
          ${ExitDo}
        ${EndIf}

        ; Reached 10 iterations (~5 s) — fall through to force-kill.
        ${If} $R6 >= 10
          ${ExitDo}
        ${EndIf}
      ${Loop}

      ; --- Step 3: force-kill fallback if either process is still alive ---
      ${If} $R4 = 0
        nsis_tauri_utils::KillProcess "Whisperstream.exe"
        Pop $R0
        nsExec::ExecToLog 'taskkill /F /T /IM Whisperstream.exe'
      ${EndIf}
      ${If} $R5 = 0
        nsis_tauri_utils::KillProcess "whisperstream-engine.exe"
        Pop $R0
        nsExec::ExecToLog 'taskkill /F /T /IM whisperstream-engine.exe'
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

; NSIS_HOOK_PREINSTALL fires inside the Install section BEFORE
; CheckIfAppIsRunning and BEFORE any files are copied (installer.nsi line 611).
; Gracefully closes the running Whisperstream instance so the installer can
; overwrite the executables without "file in use" errors.
!macro NSIS_HOOK_PREINSTALL
  !insertmacro WHISPERSTREAM_APPLY_MANAGED_LOCKDOWN_POLICY
  !insertmacro WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL
!macroend

; NSIS_HOOK_PREUNINSTALL fires inside the Uninstall section BEFORE
; CheckIfAppIsRunning and BEFORE any files are deleted (installer.nsi line 735).
; Same graceful-close sequence as PREINSTALL.
!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL
!macroend

; ---------------------------------------------------------------------------
; PATH management: $INSTDIR\bin (bin\whisperstream.exe, the CLI wrapper — see
; Task 5.1 / scripts/build.py). ONLY $INSTDIR\bin ever goes on PATH, never
; $INSTDIR itself: the install root also holds Whisperstream.exe,
; whisperstream-engine.exe, and the llama-server sidecar, and putting the
; whole root on PATH would leak all of those onto the user's shell.
;
; PATH exposure is opt-in from Agent Speak Configure/onboarding. The app adds
; this exact directory to the current user's PATH only after the user presses
; Install. The uninstaller still removes it surgically when present. Updates
; reuse the same $INSTDIR and therefore need no PATH mutation.
;
; No EnVar-style plugin is vendored for this installer, so PATH is edited by
; hand with core NSIS instructions only:
;   - WhisperstreamStrStr (classic NSIS-wiki function, duplicated below for
;     the installer and uninstaller contexts as WhisperstreamStrStr /
;     un.WhisperstreamStrStr) finds a ";"-padded needle in a ";"-padded
;     haystack, giving an exact-segment match instead of a loose substring
;     match (so e.g. "C:\Foo\bin2" never matches a search for "C:\Foo\bin").
;     The name is deliberately namespaced. Naming it plain "StrStr" collides
;     with MultiUser.nsh: Tauri's generated installer.nsi includes this file
;     first, then includes MultiUser.nsh, which calls
;     `${Using:StrFunc} StrStr` and generates a function of that exact name.
;     makensis then aborts the whole bundle step with
;     `Function named "StrStr" already exists.` Do not rename these back.
;   - The add path is idempotent: only appended when the padded needle is
;     absent.
;   - The remove path is surgical: it splices out exactly the one matched
;     ";$INSTDIR\bin;" segment and re-joins the surrounding entries with a
;     single ";", never rewriting or reordering the rest of PATH.
;   - Path is REG_EXPAND_SZ, so writes use WriteRegExpandStr, and each write
;     broadcasts WM_SETTINGCHANGE so already-open programs have a chance to
;     pick up the change; a brand new terminal window re-reads the registry
;     regardless.
!ifndef HWND_BROADCAST
  !define HWND_BROADCAST 0xFFFF
!endif
!ifndef WM_SETTINGCHANGE
  !define WM_SETTINGCHANGE 0x001A
!endif

!macro _WHISPERSTREAM_STRSTR_BODY
  Exch $R1 ; needle
  Exch     ; now haystack is on top
  Exch $R2 ; $R2 = haystack, stack top = needle (restored)
  Push $R3
  Push $R4
  Push $R5
  StrLen $R3 $R1
  StrCpy $R4 0
  ${Do}
    StrCpy $R5 $R2 $R3 $R4
    ${If} $R5 == $R1
      ${ExitDo}
    ${EndIf}
    ${If} $R5 == ""
      ${ExitDo}
    ${EndIf}
    IntOp $R4 $R4 + 1
  ${Loop}
  StrCpy $R1 $R2 "" $R4
  Pop $R5
  Pop $R4
  Pop $R3
  Pop $R2
  Exch $R1
!macroend

; Uninstaller-scoped exact-segment search (NSIS requires the "un." prefix for
; every Function called from uninstall code).
Function un.WhisperstreamStrStr
  !insertmacro _WHISPERSTREAM_STRSTR_BODY
FunctionEnd

!macro WHISPERSTREAM_PATH_REMOVE
  ${If} $UpdateMode <> 1
    SetShellVarContext current
    ReadRegStr $R6 HKCU "Environment" "Path"
    StrCpy $R7 ";$R6;"
    Push $R7
    Push ";$INSTDIR\bin;"
    Call un.WhisperstreamStrStr
    Pop $R8
    ${If} $R8 != ""
      ; $R8 = the padded string's suffix starting at the matched needle.
      ; Splice: prefix (everything before the needle) + remainder (everything
      ; after the needle), which re-joins the surviving entries around a
      ; single ";" without touching their order.
      StrCpy $R5 ";$INSTDIR\bin;"
      StrLen $R9 $R7
      StrLen $R1 $R8
      IntOp $R2 $R9 - $R1
      StrCpy $R3 $R7 $R2 0
      StrLen $R1 $R5
      StrCpy $R4 $R8 "" $R1
      StrCpy $R7 "$R3$R4"
      ; Strip the padding: one leading ";" and one trailing ";", each only if
      ; still present (the removed entry may itself have been first or last,
      ; in which case its own padding already consumed one of these).
      StrCpy $R9 $R7 1
      ${If} $R9 == ";"
        StrCpy $R7 $R7 "" 1
      ${EndIf}
      StrLen $R9 $R7
      IntOp $R2 $R9 - 1
      StrCpy $R9 $R7 1 $R2
      ${If} $R9 == ";"
        StrCpy $R7 $R7 $R2
      ${EndIf}
      WriteRegExpandStr HKCU "Environment" "Path" "$R7"
      SendMessage ${HWND_BROADCAST} ${WM_SETTINGCHANGE} 0 "STR:Environment" /TIMEOUT=5000
    ${EndIf}
  ${EndIf}
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; Always remove the Nuitka onefile extraction cache on uninstall: it is a
  ; runtime artifact (~50 MB of extracted DLLs), not user data, and it lives
  ; outside the data dir at %LOCALAPPDATA%\Whisperstream-runtime. Removing it
  ; only on a true uninstall (UpdateMode <> 1) leaves the cache intact across
  ; in-app updates so the next launch is still fast.
  ${If} $UpdateMode <> 1
    SetShellVarContext current
    RmDir /r "$LOCALAPPDATA\Whisperstream-runtime"
  ${EndIf}

  ; Optional: also remove the user data dir when the user checked the
  ; "Delete app data" box.
  ${If} $DeleteAppDataCheckboxState = 1
  ${AndIf} $UpdateMode <> 1
    SetShellVarContext current
    RmDir /r "$LOCALAPPDATA\Whisperstream"
  ${EndIf}

  ; Remove the PATH entry added from Agent Speak setup. This does NOT
  ; extend WHISPERSTREAM_GRACEFUL_CLOSE_AND_KILL and does not touch
  ; whisperstream.exe in bin\ at all: that macro force-kills two named
  ; processes, and force-killing the CLI wrapper would leave the user's
  ; console in raw mode with ENABLE_VIRTUAL_TERMINAL_INPUT set (Task 1.3,
  ; Task 3.4 — Whisperstream never TerminateProcess()es the wrapper).
  !insertmacro WHISPERSTREAM_PATH_REMOVE
!macroend
