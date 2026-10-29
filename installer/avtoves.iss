; Установщик АвтоВес (Inno Setup 6). Собирается build\build.ps1 — сначала он готовит build\stage.
;   server — служба АвтоВес (Python, распознавание номеров, весы, светофоры)
;   client — программа оператора АвтоВес.exe (окно WebView2)
#ifndef AppVer
  #define AppVer GetEnv("APP_VERSION")
#endif
#if AppVer == ""
  #define AppVer "1.0.0"
#endif
#define AppGuid "{{0C5D9E77-A226-4792-8944-7003B7B69D8E}"
#define Svc "AvtoVes"
#define DefPort "8780"

[Setup]
AppId={#AppGuid}
AppName=АвтоВес
AppVersion={#AppVer}
AppVerName=АвтоВес {#AppVer}
AppPublisher=АвтоВес
VersionInfoVersion={#AppVer}
VersionInfoDescription=Установщик АвтоВес
DefaultDirName={autopf}\AvtoVes
DefaultGroupName=АвтоВес
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
UsePreviousSetupType=yes
; служба и программа для всех пользователей — нужны права администратора
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=AvtoVes-Setup-{#AppVer}
SetupIconFile=avtoves.ico
WizardSmallImageFile=wizard-small.bmp
UninstallDisplayIcon={app}\AvtoVes.exe
UninstallDisplayName=АвтоВес
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
ru.TaskSimulate=Режим проверки без оборудования (имитатор весов, светофоров и камер)
ru.GroupOptions=Дополнительно:

[Types]
Name: "full"; Description: "Сервер весовой: весы, камеры, светофоры + программа оператора"
Name: "client"; Description: "Только программа оператора (подключается к серверу АвтоВес по сети)"

[Components]
Name: "server"; Description: "Сервер АвтоВес (служба Windows, распознавание номеров)"; Types: full
Name: "client"; Description: "Программа оператора АвтоВес"; Types: full client; Flags: fixed

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "simulate"; Description: "{cm:TaskSimulate}"; GroupDescription: "{cm:GroupOptions}"; Flags: unchecked; Components: server; Check: IsFirstInstall

[Dirs]
; база, снимки и настройки живут здесь и переживают обновление и переустановку
Name: "{commonappdata}\AvtoVes"; Permissions: users-modify; Flags: uninsneveruninstall; Components: server
Name: "{commonappdata}\AvtoVes\data"; Flags: uninsneveruninstall; Components: server
Name: "{commonappdata}\AvtoVes\logs"; Flags: uninsneveruninstall; Components: server

[InstallDelete]
; старый код приложения убираем целиком — чтобы не оставались удалённые модули
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"
; ярлык на браузер из версии 1.0
Type: files; Name: "{app}\avtoves.url"

[Files]
Source: "..\build\stage\client\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: client
Source: "..\build\stage\server\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: server
Source: "..\build\stage\redist\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: dontcopy

[Registry]
Root: HKLM; Subkey: "Software\AvtoVes"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\AvtoVes"; ValueType: string; ValueName: "Config"; ValueData: "{commonappdata}\AvtoVes\config.yaml"; Components: server
; сервер на этом компьютере — программа берёт порт из настроек; только программа — адрес сервера
Root: HKLM; Subkey: "Software\AvtoVes"; ValueType: none; ValueName: "Url"; Flags: deletevalue; Components: server
Root: HKLM; Subkey: "Software\AvtoVes"; ValueType: string; ValueName: "Url"; ValueData: "{code:GetClientUrl}"; Check: NeedClientUrl

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Icons]
Name: "{group}\АвтоВес"; Filename: "{app}\AvtoVes.exe"; Comment: "Рабочее место оператора весовой"; AppUserModelID: "AvtoVes.Client"
Name: "{group}\Настройки АвтоВес"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\AvtoVes\config.yaml"""; IconFilename: "{app}\avtoves.ico"; Components: server
Name: "{group}\Перезапустить АвтоВес"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -Command ""Start-Process -FilePath '{app}\AvtoVesService.exe' -ArgumentList 'restart' -Verb RunAs -WindowStyle Hidden"""; IconFilename: "{app}\avtoves.ico"; Comment: "Применить изменённые настройки"; Components: server
Name: "{group}\Данные и снимки"; Filename: "{commonappdata}\AvtoVes\data"; Components: server
Name: "{group}\Журналы"; Filename: "{commonappdata}\AvtoVes\logs"; Components: server
Name: "{group}\Обмен с 1С"; Filename: "{app}\1C"; Components: server
Name: "{group}\Инструкция"; Filename: "{win}\notepad.exe"; Parameters: """{app}\README.md"""
Name: "{group}\{cm:UninstallProgram,АвтоВес}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\АвтоВес"; Filename: "{app}\AvtoVes.exe"; Tasks: desktopicon; AppUserModelID: "AvtoVes.Client"

[Run]
Filename: "{app}\AvtoVes.exe"; Description: "Запустить АвтоВес"; Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
var
  HwPage, CamPage, ClientPage: TInputQueryWizardPage;
  ModePage: TInputOptionWizardPage;
  ConfigExisted, ClientUrlExisted: Boolean;
  FinalPort: String;

function ConfigPath(): String;
begin
  Result := ExpandConstant('{commonappdata}\AvtoVes\config.yaml');
end;

function IsServer(): Boolean;
begin
  Result := WizardIsComponentSelected('server');
end;

function IsFirstInstall(): Boolean;
begin
  Result := not ConfigExisted;
end;

function NeedClientUrl(): Boolean;
begin
  Result := (not IsServer()) and (not ClientUrlExisted);
end;

function GetClientUrl(Param: String): String;
var
  S: String;
begin
  S := Trim(ClientPage.Values[0]);
  if (Pos('http://', LowerCase(S)) <> 1) and (Pos('https://', LowerCase(S)) <> 1) then
  begin
    // без порта — порт по умолчанию
    if Pos(':', S) = 0 then S := S + ':{#DefPort}';
    S := 'http://' + S;
  end;
  if Copy(S, Length(S), 1) <> '/' then S := S + '/';
  Result := S;
end;

// ------------------------------------------------------------ защита от отката (как в Ласточке)
function InitializeSetup(): Boolean;
var
  Installed, Url: String;
  Cur, Old: Int64;
begin
  Result := True;
  ConfigExisted := FileExists(ConfigPath());
  ClientUrlExisted := RegQueryStringValue(HKLM64, 'Software\AvtoVes', 'Url', Url) and (Url <> '');
  FinalPort := '';
  if Pos('/ALLOWDOWNGRADE', UpperCase(GetCmdTail)) > 0 then Exit;
  if not RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{0C5D9E77-A226-4792-8944-7003B7B69D8E}_is1', 'DisplayVersion', Installed) then Exit;
  if not StrToVersion('{#AppVer}', Cur) then Exit;
  if not StrToVersion(Installed, Old) then Exit;
  if ComparePackedVersion(Cur, Old) < 0 then
  begin
    if not WizardSilent then
      MsgBox('Уже установлен более новый АвтоВес (' + Installed + '). Установка старой версии ({#AppVer}) отменена.', mbError, MB_OK);
    Result := False;
  end;
end;

// ------------------------------------------------------------ страницы мастера
procedure InitializeWizard();
begin
  HwPage := CreateInputQueryPage(wpSelectTasks,
    'Подключение оборудования', 'Весы и светофоры',
    'Укажите адреса модулей Moxa в сети весовой. Позже их можно изменить в «Настройках АвтоВес».');
  HwPage.Add('IP-адрес Moxa NPort (весовой индикатор CAS CI-200A):', False);
  HwPage.Add('TCP-порт NPort (режим TCP Server):', False);
  HwPage.Add('IP-адрес Moxa ioLogik для светофоров (пусто — без светофоров):', False);
  HwPage.Add('Порт АвтоВес в сети (должен быть свободен):', False);
  HwPage.Values[0] := ExpandConstant('{param:NPORT|192.168.1.50}');
  HwPage.Values[1] := ExpandConstant('{param:NPORTPORT|4001}');
  HwPage.Values[2] := ExpandConstant('{param:IOLOGIK|192.168.1.60}');
  HwPage.Values[3] := ExpandConstant('{param:PORT|{#DefPort}}');

  CamPage := CreateInputQueryPage(HwPage.ID,
    'Камеры', 'Камеры распознавания номеров',
    'RTSP-адреса камер, которые смотрят на въезды. Пустое поле — камеры нет.' + #13#10 +
    'Пример для Hikvision: rtsp://логин:пароль@IP:554/Streaming/Channels/101');
  CamPage.Add('Камера въезда А:', False);
  CamPage.Add('Камера въезда Б:', False);
  CamPage.Values[0] := ExpandConstant('{param:CAMA|}');
  CamPage.Values[1] := ExpandConstant('{param:CAMB|}');

  ModePage := CreateInputOptionPage(CamPage.ID,
    'Режим проезда', 'Как машины проезжают через весы?',
    'От этого зависит, какой светофор включается на выезд после взвешивания.', True, False);
  ModePage.Add('Проездные весы — въезд с одной стороны, выезд с другой');
  ModePage.Add('Тупиковые весы — въезд и выезд со стороны А');
  if ExpandConstant('{param:MODE|through}') = 'reverse' then
    ModePage.SelectedValueIndex := 1
  else
    ModePage.SelectedValueIndex := 0;

  ClientPage := CreateInputQueryPage(ModePage.ID,
    'Сервер АвтоВес', 'К какому серверу подключаться?',
    'Укажите IP-адрес или имя компьютера весовой, где установлен сервер АвтоВес. ' +
    'Порт можно указать через двоеточие (по умолчанию {#DefPort}).');
  ClientPage.Add('Адрес сервера (например 192.168.1.10 или 192.168.1.10:{#DefPort}):', False);
  ClientPage.Values[0] := ExpandConstant('{param:SERVER|}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // вопросы об оборудовании — только при первой установке сервера
  if (PageID = HwPage.ID) or (PageID = CamPage.ID) or (PageID = ModePage.ID) then
    Result := ConfigExisted or not IsServer();
  if PageID = ClientPage.ID then
    Result := IsServer() or ClientUrlExisted;
end;

function ValidPort(S: String): Boolean;
var
  N: Integer;
begin
  N := StrToIntDef(Trim(S), -1);
  Result := (N >= 1) and (N <= 65535);
end;

function PS(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function PortBusy(Port: String): Boolean;
var
  RC: Integer;
begin
  Result := False;
  if Exec(PS(), '-NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort ' + Trim(Port) +
    ' -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"', '', SW_HIDE, ewWaitUntilTerminated, RC) then
    Result := RC = 1;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = HwPage.ID then
  begin
    if (Trim(HwPage.Values[0]) = '') and not WizardIsTaskSelected('simulate') then
    begin
      MsgBox('Укажите IP-адрес Moxa NPort.', mbError, MB_OK);
      Result := False;
    end
    else if not ValidPort(HwPage.Values[1]) or not ValidPort(HwPage.Values[3]) then
    begin
      MsgBox('Порт — число от 1 до 65535.', mbError, MB_OK);
      Result := False;
    end
    else if PortBusy(HwPage.Values[3]) then
    begin
      MsgBox('Порт ' + Trim(HwPage.Values[3]) + ' уже занят другой программой на этом компьютере ' +
        '(например, детектором людей или другим веб-сервисом).' + #13#10 + 'Укажите другой порт, например {#DefPort} или 8781.',
        mbError, MB_OK);
      Result := False;
    end;
  end;
  if CurPageID = ClientPage.ID then
  begin
    if Trim(ClientPage.Values[0]) = '' then
    begin
      MsgBox('Укажите адрес сервера АвтоВес.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
begin
  S := MemoDirInfo + NewLine + NewLine + MemoTypeInfo + NewLine + NewLine;
  if not IsServer() then
  begin
    if ClientUrlExisted then
      S := S + 'Обновление программы оператора, адрес сервера сохранится.' + NewLine
    else
      S := S + 'Сервер АвтоВес: ' + GetClientUrl('') + NewLine;
  end
  else if ConfigExisted then
    S := S + 'Обновление: настройки, база взвешиваний и снимки сохранятся.' + NewLine +
             Space + ConfigPath() + NewLine
  else
  begin
    S := S + 'Оборудование:' + NewLine +
      Space + 'Весы (NPort): ' + HwPage.Values[0] + ':' + HwPage.Values[1] + NewLine;
    if Trim(HwPage.Values[2]) <> '' then
      S := S + Space + 'Светофоры (ioLogik): ' + HwPage.Values[2] + NewLine
    else
      S := S + Space + 'Светофоры: не подключены' + NewLine;
    if Trim(CamPage.Values[0]) <> '' then S := S + Space + 'Камера А: задана' + NewLine;
    if Trim(CamPage.Values[1]) <> '' then S := S + Space + 'Камера Б: задана' + NewLine;
    if ModePage.SelectedValueIndex = 1 then
      S := S + Space + 'Режим: тупиковые весы' + NewLine
    else
      S := S + Space + 'Режим: проездные весы' + NewLine;
    S := S + Space + 'Порт АвтоВес: ' + HwPage.Values[3] + NewLine;
  end;
  if MemoTasksInfo <> '' then
    S := S + NewLine + MemoTasksInfo + NewLine;
  if IsServer() then
    S := S + NewLine + 'Сервер АвтоВес будет работать как служба Windows и запускаться вместе с компьютером.';
  Result := S;
end;

// ------------------------------------------------------------ служба
function RunCmd(const Exe, Params: String; Hide: Boolean): Integer;
var
  RC, Show: Integer;
begin
  if Hide then Show := SW_HIDE else Show := SW_SHOWNORMAL;
  if not Exec(Exe, Params, '', Show, ewWaitUntilTerminated, RC) then
    RC := -1;
  Log(Exe + ' ' + Params + ' -> ' + IntToStr(RC));
  Result := RC;
end;

procedure StopAndRemoveService();
var
  Wrapper: String;
begin
  Wrapper := ExpandConstant('{app}\AvtoVesService.exe');
  if FileExists(Wrapper) then
  begin
    RunCmd(Wrapper, 'stop', True);
    RunCmd(Wrapper, 'uninstall', True);
  end
  else
  begin
    RunCmd(ExpandConstant('{sys}\sc.exe'), 'stop {#Svc}', True);
    Sleep(3000);
    RunCmd(ExpandConstant('{sys}\sc.exe'), 'delete {#Svc}', True);
  end;
  // на всякий случай — процессы Python из папки программы (иначе файлы заняты)
  RunCmd(PS(), '-NoProfile -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -like ''' + ExpandConstant('{app}') + '\*'' } | Stop-Process -Force"', True);
  Sleep(1000);
end;

// Программа оператора может быть открыта у нескольких пользователей (RDP) — закрываем все копии
procedure CloseClients();
begin
  RunCmd(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AvtoVes.exe', True);
end;

procedure WaitForWeb(Port: String);
begin
  RunCmd(PS(), '-NoProfile -Command "for($i=0;$i -lt 60;$i++){try{$c=New-Object Net.Sockets.TcpClient(''127.0.0.1'',' + Port +
    ');$c.Close();exit 0}catch{Start-Sleep -Milliseconds 500}};exit 1"', True);
end;

function PyRun(const Params: String): Integer;
begin
  Result := RunCmd(ExpandConstant('{app}\python\python.exe'),
    '-X utf8 "' + ExpandConstant('{app}\app\setup_config.py') + '" ' + Params, True);
end;

function ReportFile(): String;
begin
  Result := ExpandConstant('{tmp}\avtoves-port.txt');
end;

function ReadReportedPort(): String;
var
  S: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(ReportFile(), S) then
    Result := Trim(String(S));
end;

procedure CreateConfig();
var
  Params, Mode: String;
  RC: Integer;
begin
  if ModePage.SelectedValueIndex = 1 then Mode := 'reverse' else Mode := 'through';
  Params := '--template "' + ExpandConstant('{app}\config.template.yaml') + '"' +
    ' --out "' + ConfigPath() + '"' +
    ' --report "' + ReportFile() + '"' +
    ' --nport "' + Trim(HwPage.Values[0]) + '"' +
    ' --nport-port "' + Trim(HwPage.Values[1]) + '"' +
    ' --iologik "' + Trim(HwPage.Values[2]) + '"' +
    ' --web-port "' + Trim(HwPage.Values[3]) + '"' +
    ' --cam-a "' + Trim(CamPage.Values[0]) + '"' +
    ' --cam-b "' + Trim(CamPage.Values[1]) + '"' +
    ' --mode ' + Mode;
  if WizardIsTaskSelected('simulate') then
    Params := Params + ' --simulate';
  RC := PyRun(Params);
  if (RC <> 0) and not WizardSilent then
    MsgBox('Не удалось создать файл настроек (код ' + IntToStr(RC) + ').' + #13#10 +
      'Проверьте введённые адреса и отредактируйте ' + ConfigPath() + ' вручную.', mbError, MB_OK);
end;

// Обновление: если порт АвтоВес занят другой программой (например, детектором людей) — переводим на свободный
procedure FixPort();
var
  RC: Integer;
begin
  RC := PyRun('--fix-port --out "' + ConfigPath() + '" --report "' + ReportFile() + '"');
  if (RC = 10) and not WizardSilent then
    MsgBox('Прежний порт АвтоВес занят другой программой на этом компьютере.' + #13#10 +
      'АвтоВес переведён на порт ' + ReadReportedPort() + '. Программа АвтоВес на этом компьютере подключится сама; ' +
      'на других компьютерах и в 1С укажите новый порт.', mbInformation, MB_OK);
end;

function WebView2Installed(): Boolean;
var
  V: String;
begin
  Result :=
    (RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', V) and (V <> '') and (V <> '0.0.0.0'));
end;

// Среда WebView2 ставится в фоне: ждём не больше 2 минут, остальное установщик Microsoft доделает сам.
// Пока её нет, программа откроется окном Microsoft Edge.
procedure EnsureWebView2();
var
  RC, I: Integer;
begin
  if WebView2Installed() then
  begin
    Log('WebView2 уже установлен');
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Установка компонента Microsoft WebView2...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  // копия во временную папку Windows: {tmp} удаляется в конце установки, а установщик WebView2 может работать дольше
  FileCopy(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), ExpandConstant('{win}\Temp\AvtoVes-WebView2Setup.exe'), False);
  if not Exec(ExpandConstant('{win}\Temp\AvtoVes-WebView2Setup.exe'), '/silent /install', '', SW_HIDE, ewNoWait, RC) then
  begin
    Log('WebView2: установщик не запустился');
    Exit;
  end;
  for I := 1 to 60 do
  begin
    if WebView2Installed() then
    begin
      Log('WebView2 установлен');
      Exit;
    end;
    Sleep(2000);
  end;
  Log('WebView2 ещё устанавливается в фоне');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Wrapper, Py: String;
begin
  if CurStep = ssInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Закрытие АвтоВес...';
    CloseClients();
    if IsServer() or FileExists(ExpandConstant('{app}\AvtoVesService.exe')) then
      StopAndRemoveService();
  end;
  if CurStep = ssPostInstall then
  begin
    EnsureWebView2();
    if not IsServer() then Exit;
    Wrapper := ExpandConstant('{app}\AvtoVesService.exe');
    Py := ExpandConstant('{app}\python\python.exe');
    if ConfigExisted then
    begin
      WizardForm.StatusLabel.Caption := 'Проверка порта АвтоВес...';
      FixPort();
    end
    else
    begin
      WizardForm.StatusLabel.Caption := 'Создание настроек...';
      CreateConfig();
    end;
    FinalPort := ReadReportedPort();
    if FinalPort <> '' then
      RegWriteStringValue(HKLM64, 'Software\AvtoVes', 'Port', FinalPort);
    WizardForm.StatusLabel.Caption := 'Разрешение в брандмауэре Windows...';
    RunCmd(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="АвтоВес"', True);
    RunCmd(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall add rule name="АвтоВес" dir=in action=allow ' +
      'program="' + Py + '" enable=yes profile=any', True);
    WizardForm.StatusLabel.Caption := 'Запуск службы АвтоВес...';
    if RunCmd(Wrapper, 'install', True) <> 0 then
      Log('Служба: установка вернула ошибку');
    if RunCmd(Wrapper, 'start', True) <> 0 then
    begin
      if not WizardSilent then
        MsgBox('Служба АвтоВес не запустилась. Журнал: ' +
          ExpandConstant('{commonappdata}\AvtoVes\logs'), mbError, MB_OK);
    end
    else if FinalPort <> '' then
      WaitForWeb(FinalPort);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and IsServer() and (FinalPort <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'АвтоВес работает на порту ' + FinalPort + '.' + #13#10 +
      'На других компьютерах установите «Только программа оператора» и укажите адрес ' +
      'этого компьютера: <IP>:' + FinalPort;
end;

// ------------------------------------------------------------ удаление
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    RunCmd(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AvtoVes.exe', True);
    if FileExists(ExpandConstant('{app}\AvtoVesService.exe')) then
    begin
      StopAndRemoveService();
      RunCmd(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="АвтоВес"', True);
    end;
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{commonappdata}\AvtoVes');
    if DirExists(Data) and not UninstallSilent then
      if MsgBox('Удалить также базу взвешиваний, снимки и настройки?' + #13#10 + Data + #13#10#13#10 +
        'Если планируете переустановить АвтоВес — нажмите «Нет».', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
