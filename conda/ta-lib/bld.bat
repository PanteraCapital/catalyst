rmdir /s /q "C:\ta-lib\"
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('https://downloads.sourceforge.net/project/ta-lib/ta-lib/0.4.0/ta-lib-0.4.0-msvc.zip', 'ta-lib-0.4.0-msvc.zip'); if ((Get-FileHash -Algorithm SHA256 'ta-lib-0.4.0-msvc.zip').Hash -ne '2dae7a1391222f0a7e601ecda654f93cebedfdb37afeb30df2721a3b0a717875') { Remove-Item 'ta-lib-0.4.0-msvc.zip'; Write-Error 'TA-Lib archive checksum mismatch' }"
if errorlevel 1 exit 1
powershell -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem;[System.IO.Compression.ZipFile]::ExtractToDirectory('ta-lib-0.4.0-msvc.zip', 'C:\')"
IF %ERRORLEVEL% == 1; exit 1
pushd C:\ta-lib\c\
pushd make\cdd\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
pushd make\cdr\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
pushd make\cmd\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
pushd make\cmr\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
pushd make\csd\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
pushd make\csr\win32\msvc
nmake
IF %ERRORLEVEL% == 1; exit 1
popd
popd
del ta-lib-0.4.0-msvc.zip

python setup.py build --compiler msvc
python setup.py install --prefix=%PREFIX%
