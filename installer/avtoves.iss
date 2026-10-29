; Установщик АвтоВес (Inno Setup 6). Собирается build\build.ps1 — сначала он готовит build\stage.
#ifndef AppVer
  #define AppVer GetEnv("APP_VERSION")
#endif
#if AppVer == ""
  #define AppVer "1.0.0"
#endif
#define AppGuid "{{0C5D9E77-A226-4792-8944-7003B7B69D8E}"
#define Svc "AvtoVes"

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
; служба для всех пользователей — нужны права администратора
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=AvtoVes-Setup-{#AppVer}
SetupIconFile=avtoves.ico
WizardSmallImageFile=wizard-small.bmp
UninstallDisplayIcon={app}\avtoves.ico
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

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "simulate"; Description: "{cm:TaskSimulate}"; GroupDescription: "{cm:GroupOptions}"; Flags: unchecked; Check: IsFirstInstall

[Dirs]
; база, снимки и настройки живут здесь и переживают обновление и переустановку
Name: "{commonappdata}\AvtoVes"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AvtoVes\data"; Flags: uninsneveruninstall
Name: "{commonappdata}\AvtoVes\logs"; Flags: uninsneveruninstall

[InstallDelete]
; старый код приложения убираем целиком — чтобы не оставались удалённые модули
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Files]
Source: "..\build\stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
Root: HKLM; Subkey: "Software\AvtoVes"; ValueType: string; ValueName: "Port"; ValueData: "{code:GetPort}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\AvtoVes"; ValueType: string; ValueName: "Config"; ValueData: "{commonappdata}\AvtoVes\config.yaml"

[INI]
; ярлык на веб-интерфейс (порт берётся из установки)
Filename: "{app}\avtoves.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://localhost:{code:GetPort}/"
Filename: "{app}\avtoves.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\avtoves.ico"
Filename: "{app}\avtoves.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"

[UninstallDelete]
Type: files; Name: "{app}\avtoves.url"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"

[Icons]
Name: "{group}\АвтоВес"; Filename: "{app}\avtoves.url"; IconFilename: "{app}\avtoves.ico"; Comment: "Рабочее место оператора весовой"
Name: "{group}\Настройки АвтоВес"; Filename: "{win}\notepad.exe"; Parameters: """{commonappdata}\AvtoVes\config.yaml"""; IconFilename: "{app}\avtoves.ico"
Name: "{group}\Перезапустить АвтоВес"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -Command ""Start-Process -FilePath '{app}\AvtoVesService.exe' -ArgumentList 'restart' -Verb RunAs -WindowStyle Hidden"""; IconFilename: "{app}\avtoves.ico"; Comment: "Применить изменённые настройки"
Name: "{group}\Данные и снимки"; Filename: "{commonappdata}\AvtoVes\data"
Name: "{group}\Журналы"; Filename: "{commonappdata}\AvtoVes\logs"
Name: "{group}\Обмен с 1С"; Filename: "{app}\1C"
Name: "{group}\Инструкция"; Filename: "{win}\notepad.exe"; Parameters: """{app}\README.md"""
Name: "{group}\{cm:UninstallProgram,АвтоВес}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\АвтоВес"; Filename: "{app}\avtoves.url"; IconFilename: "{app}\avtoves.ico"; Tasks: desktopicon

[Run]
Filename: "http://localhost:{code:GetPort}/"; Description: "Открыть АвтоВес"; Flags: shellexec postinstall nowait skipifsilent

[Code]
var
  HwPage, CamPage: TInputQueryWizardPage;
  ModePage: TInputOptionWizardPage;
  ConfigExisted: Boolean;

function ConfigPath(): String;
begin
  Result := ExpandConstant('{commonappdata}\AvtoVes\config.yaml');
end;

function IsFirstInstall(): Boolean;
begin
  Result := not ConfigExisted;
end;

function GetPort(Param: String): String;
begin
  Result := '';
  if ConfigExisted then
    RegQueryStringValue(HKLM64, 'Software\AvtoVes', 'Port', Result);
  if (Result = '') and (HwPage <> nil) then
    Result := Trim(HwPage.Values[3]);
  if Result = '' then
    Result := '8080';
end;

// ------------------------------------------------------------ защита от отката (как в Ласточке)
function InitializeSetup(): Boolean;
var
  Installed: String;
  Cur, Old: Int64;
begin
  Result := True;
  ConfigExisted := FileExists(ConfigPath());
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
  HwPage.Add('Порт веб-интерфейса АвтоВес:', False);
  HwPage.Values[0] := ExpandConstant('{param:NPORT|192.168.1.50}');
  HwPage.Values[1] := ExpandConstant('{param:NPORTPORT|4001}');
  HwPage.Values[2] := ExpandConstant('{param:IOLOGIK|192.168.1.60}');
  HwPage.Values[3] := ExpandConstant('{param:PORT|8080}');

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
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // при обновлении настройки уже есть — вопросы не задаём
  Result := ConfigExisted and ((PageID = HwPage.ID) or (PageID = CamPage.ID) or (PageID = ModePage.ID));
end;

function ValidPort(S: String): Boolean;
var
  N: Integer;
begin
  N := StrToIntDef(Trim(S), -1);
  Result := (N >= 1) and (N <= 65535);
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
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var
  S: String;
begin
  S := MemoDirInfo + NewLine + NewLine;
  if ConfigExisted then
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
    S := S + Space + 'Веб-интерфейс: http://<этот компьютер>:' + HwPage.Values[3] + NewLine;
  end;
  if MemoTasksInfo <> '' then
    S := S + NewLine + MemoTasksInfo + NewLine;
  S := S + NewLine + 'АвтоВес будет установлен как служба Windows и будет запускаться вместе с компьютером.';
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

function PS(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
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

procedure WaitForWeb(Port: String);
begin
  RunCmd(PS(), '-NoProfile -Command "for($i=0;$i -lt 60;$i++){try{$c=New-Object Net.Sockets.TcpClient(''127.0.0.1'',' + Port +
    ');$c.Close();exit 0}catch{Start-Sleep -Milliseconds 500}};exit 1"', True);
end;

procedure CreateConfig();
var
  Params, Mode, Py: String;
  RC: Integer;
begin
  if ModePage.SelectedValueIndex = 1 then Mode := 'reverse' else Mode := 'through';
  Py := ExpandConstant('{app}\python\python.exe');
  Params := '-X utf8 "' + ExpandConstant('{app}\app\setup_config.py') + '"' +
    ' --template "' + ExpandConstant('{app}\config.template.yaml') + '"' +
    ' --out "' + ConfigPath() + '"' +
    ' --nport "' + Trim(HwPage.Values[0]) + '"' +
    ' --nport-port "' + Trim(HwPage.Values[1]) + '"' +
    ' --iologik "' + Trim(HwPage.Values[2]) + '"' +
    ' --web-port "' + Trim(HwPage.Values[3]) + '"' +
    ' --cam-a "' + Trim(CamPage.Values[0]) + '"' +
    ' --cam-b "' + Trim(CamPage.Values[1]) + '"' +
    ' --mode ' + Mode;
  if WizardIsTaskSelected('simulate') then
    Params := Params + ' --simulate';
  RC := RunCmd(Py, Params, True);
  if (RC <> 0) and not WizardSilent then
    MsgBox('Не удалось создать файл настроек (код ' + IntToStr(RC) + ').' + #13#10 +
      'Проверьте введённые адреса и отредактируйте ' + ConfigPath() + ' вручную.', mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Wrapper, Py: String;
begin
  if CurStep = ssInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Остановка службы АвтоВес...';
    StopAndRemoveService();
  end;
  if CurStep = ssPostInstall then
  begin
    Wrapper := ExpandConstant('{app}\AvtoVesService.exe');
    Py := ExpandConstant('{app}\python\python.exe');
    if not ConfigExisted then
    begin
      WizardForm.StatusLabel.Caption := 'Создание настроек...';
      CreateConfig();
    end;
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
    else
      WaitForWeb(GetPort(''));
  end;
end;

// ------------------------------------------------------------ удаление
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    StopAndRemoveService();
    RunCmd(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name="АвтоВес"', True);
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
