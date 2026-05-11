; mAistro — Inno Setup script
;
; Builds a Windows installer wrapping the PyInstaller one-folder bundle
; produced by `pyinstaller maistro.spec`. The installer:
;   - drops dist\maistro\* into Program Files\mAistro\
;   - registers Start-Menu and (optional) Desktop shortcuts
;   - keeps per-user app-data (recent projects, templates) at
;     %APPDATA%\mAistro\, untouched by uninstall
;
; Build (with Inno Setup 6 installed):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\maistro.iss
;
; Or use installer\build.ps1 which runs PyInstaller + ISCC end-to-end.
;
; The Claude CLI is *not* bundled — mAistro detects it on PATH at startup
; and prints a non-fatal warning if missing. The README and a post-install
; message inform the user.

#define AppName "mAistro"
#define AppPublisher "mAistro"
#define AppExeName "maistro.exe"
#define AppURL "https://claude.ai/code"
#ifndef AppVersion
#define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6F3A8E2B-7A5C-4D1B-9C3E-2B5F1E0A4D77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=maistro-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=
ChangesAssociations=no
CloseApplications=force
RestartApplicationsIfNeeded=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Bundle root: dist\maistro\maistro.exe + dist\maistro\_internal\*
Source: "..\dist\maistro\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\maistro\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The bundle's own files get removed automatically. Per-user app-data
; under %APPDATA%\mAistro is intentionally left in place — it holds the
; user's recent-projects list and job templates.

[Messages]
FinishedHeadingLabel=Setup is complete.
FinishedLabelNoIcons=mAistro has been installed.%n%nNote: you also need the Claude Code CLI installed and authenticated for agents to dispatch. See https://claude.ai/code for installation instructions.
FinishedLabel=mAistro has been installed. You can launch it from the [name] icon you just created.%n%nNote: you also need the Claude Code CLI installed and authenticated for agents to dispatch. See https://claude.ai/code for installation instructions.
