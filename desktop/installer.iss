; AutoMind 桌面版安装器（Inno Setup 6，中文向导）
;   前置：pyinstaller automind.spec 产出 dist\AutoMind\
;   构建：iscc installer.iss  →  Output\AutoMind-Setup-<ver>.exe

#define AppName "AutoMind"
#define AppVersion "1.2.0"
#define AppPublisher "AutoMind Team"
#define AppURL "https://github.com/yl13571844594-arch/AutoMind"
#define AppExe "AutoMind.exe"

[Setup]
AppId={{8F3A9C1E-6D2B-4E7A-9B5C-AUTOMIND0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
OutputBaseFilename=AutoMind-Setup-{#AppVersion}
SetupIconFile=icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\AutoMind\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs
; WebView2 Evergreen Bootstrapper（~2MB，微软官方；缺运行时的精简系统静默补装）
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall external skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 检测到缺 WebView2 时静默安装（HKLM/HKCU 任一存在即跳过）
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "正在安装 Microsoft WebView2 运行时…"; Check: WebView2Missing; Flags: skipifdoesntexist
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 程序目录内运行残留（用户数据在 %APPDATA%\AutoMind，由下方 Code 段询问）
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
function WebView2Missing: Boolean;
begin
  Result := not (
    RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') or
    RegKeyExists(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\AutoMind');
    if DirExists(DataDir) then
      if MsgBox('是否同时删除用户数据（配置、API Key、知识库、任务历史）？'#13#10 +
                DataDir + #13#10#13#10 + '选择「否」将保留数据，重装后可继续使用。',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
