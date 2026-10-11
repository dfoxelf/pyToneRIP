# =============================================================================
# pyToneRIP Package Factory
# =============================================================================
# Этот сценарий является единой точкой production/test-сборки модульного набора
# pyToneRIP для Windows 11. Он не содержит логики RIP или описаний принтеров.
# Его задача — оркестрировать уже существующие инструменты в строгом порядке:
#
#   1. собрать общий self-contained Python/Cython runtime;
#   2. попросить Python registry сгенерировать capabilities, PDC/PDR и catalog;
#   3. собрать один Manager MSIX со всеми моделями;
#   4. подписать MSIX проектным сертификатом;
#   5. проверить identity и границы пакетов;
#   6. дополнить catalog путями, SHA-256 и dependencies;
#   7. подписать сам catalog и выполнить итоговый независимый аудит.
#
# Такая фабрика держит один package identity и один C#/XAML проект. При создании
# очереди VirtualPrinterManager выбирает из него PDC/PDR конкретной модели.
# =============================================================================

# CmdletBinding превращает обычный ps1 в advanced script: PowerShell корректно
# обрабатывает именованные параметры, -Verbose и стандартные правила binding.
[CmdletBinding()]
param(
    # Версия сразу всего ModuleSet. Формат совпадает с обязательным форматом
    # MSIX Identity.Version: четыре целых компонента через точку.
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+\.\d+$')]
    [string]$Version,

    # Использовать уже существующий windows_psa/runtime/dist. Допустимо только
    # когда Python/Cython исходники не менялись. После изменения RIP или Cython
    # этот switch применять нельзя: иначе MSIX получит старый backend.
    [switch]$SkipPythonRuntimeBuild,

    # Продолжить именно эту же версию после частичного сбоя. Уже созданный и
    # подписанный пакет повторно не компилируется. Без -Resume существующий
    # artifact-каталог считается защитой от случайной перезаписи rollback.
    [switch]$Resume,

    # Выполнить только реальную пробную подпись через внешний SignTool и выйти.
    # Используется для быстрой проверки доступа к PFX без сборки runtime/MSIX.
    [switch]$SigningPreflightOnly,

    # Необязательный нестандартный каталог результата. Обычная production/test
    # сборка всегда оставляет его пустым и пишет в windows_psa/artifacts.
    [string]$OutputRoot,

    # Test builds may use an existing private signing directory outside the
    # publishable source tree. The default remains the project-local location.
    [string]$SigningRoot
)

# Запрещаем неинициализированные переменные и несуществующие свойства. Для
# build script лучше немедленный явный сбой, чем повреждённый неполный пакет.
Set-StrictMode -Version Latest
# Любая PowerShell-ошибка становится terminating exception и останавливает gate.
$ErrorActionPreference = "Stop"

# Каталог самого сценария: ...\xp750-rip\windows_psa.
$scriptRoot = $PSScriptRoot
# Корень репозитория на один уровень выше windows_psa.
$repositoryRoot = Split-Path -Parent $scriptRoot
# Единственный C#/XAML UWP проект Manager с единственным статическим manifest.
# При package-сборке он получает свежие generated assets через MSBuild property.
$projectPath = Join-Path $scriptRoot "pyToneRIP\pyToneRIP.csproj"
# Проектный Python из .venv: гарантирует версии зависимостей из uv.lock.
$pythonPath = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
# Отдельный сценарий создаёт x64 AOT Cython extension и PyInstaller one-folder.
$runtimeBuild = Join-Path $scriptRoot "Build-PythonRuntime.ps1"
# Временная рабочая зона конкретной версии. Это не выдаваемый пользователю
# artifact и не место для ручного редактирования исходников.
$workRoot = Join-Path $repositoryRoot "tmp\package-factory-$Version"
# Здесь Python generator создаёт только capabilities/PDC/PDR/catalog.
$generatedRoot = Join-Path $workRoot "generated"
# Изолированные bin/obj каждого пакета. Изоляция исключает смешивание identities
# и stale QueueHelper/WinMD между последовательными вызовами MSBuild.
$buildRoot = Join-Path $workRoot "build"
# Финальный каталог, который целиком копируется на VM. По умолчанию версия
# является частью имени, поэтому предыдущие рабочие сборки не перезаписываются.
$artifactRoot = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    Join-Path $scriptRoot "artifacts\pyToneRIP-ModuleSet-$Version"
} else {
    [System.IO.Path]::GetFullPath($OutputRoot)
}
# Временный catalog от generator: пока без путей и хешей ещё не созданных MSIX.
$catalogPath = Join-Path $generatedRoot "catalog.json"
# Финальный catalog рядом с пакетами; Manager читает именно его.
$publishedCatalogPath = Join-Path $artifactRoot "catalog.json"
# Этот маркер отличает полностью проверенный переносимый релиз от промежуточной
# папки, которая могла остаться после остановки MSBuild/SignTool. Установщик и
# независимый аудит отказываются использовать ModuleSet без валидного маркера.
$readyMarkerPath = Join-Path $artifactRoot "PACKAGE-READY.json"
# Publisher обязан byte-for-byte совпадать во всех manifests и сертификате.
$publisher = "CN=pyToneRIP"
# Extended Key Usage «Code Signing». Без него SignTool/Windows не считают
# сертификат пригодным для подписи программного пакета.
$signingEku = "1.3.6.1.5.5.7.3.3"
# Постоянное проектное хранилище тестовой подписи. Оно намеренно не находится во
# временном каталоге, чтобы каждая версия не создавала новый недоверенный root.
$localSigningRoot = if ([string]::IsNullOrWhiteSpace($SigningRoot)) {
    Join-Path $scriptRoot "signing-local"
} else {
    [System.IO.Path]::GetFullPath($SigningRoot)
}
# PFX содержит сертификат и закрытый ключ, используемый только при сборке.
$localPfxPath = Join-Path $localSigningRoot "pyToneRIP-Development.pfx"
# Случайный пароль PFX хранится отдельно рядом; оба файла образуют одну пару.
$localPasswordPath = Join-Path $localSigningRoot "pyToneRIP-Development.password"
# Каждый дочерний Python/uv процесс обязан использовать локальный cache проекта.
# Присваивание стоит до первого child process, чтобы uv никогда не пытался писать
# в C:\Users\...\AppData\Local\uv\cache, где sandbox уже давал Access Denied.
$env:UV_CACHE_DIR = Join-Path $repositoryRoot ".uv-cache"
# Build hosts can use an ANSI console code page that cannot represent the
# Russian diagnostics emitted by the Python CLI.  Keep child-process output
# UTF-8 so package generation does not fail while reporting a real error.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Возвращает абсолютный путь к MSBuild.exe или останавливает сборку.
function Find-MSBuild {
    # vswhere — официальный способ найти фактически установленный Visual Studio
    # или Build Tools, не предполагая конкретный edition и каталог установки.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere) {
        # -products * включает полноценную VS и отдельный Build Tools. Берём
        # только самый новый найденный MSBuild, потому что UWP targets зависят от VS.
        $candidate = & $vswhere -latest -products * -find "MSBuild\**\Bin\MSBuild.exe" |
            Select-Object -First 1
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    # Предсказуемый fallback для стандартной установки VS 2022 Build Tools.
    $fallback = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe"
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }
    throw "MSBuild.exe was not found. Install Visual Studio 2022 UWP build tools."
}

# Возвращает x64 SignTool из наиболее нового установленного Windows SDK.
function Find-SignTool {
    # SignTool поставляется Windows SDK, а не Visual Studio C# compiler.
    $sdkBin = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    # SDK допускает несколько side-by-side версий. Сортируем номера по убыванию
    # и выбираем первый существующий x64 SignTool.
    $candidate = Get-ChildItem -LiteralPath $sdkBin -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if ($candidate) { return $candidate }
    throw "x64 signtool.exe was not found in the Windows SDK."
}

# Создаёт при первом запуске или загружает постоянный project-local сертификат.
# Возвращаемый объект содержит открытый X509 object и пароль PFX.
function Get-LocalDevelopmentCertificate {
    # PFX и password должны существовать либо вместе, либо отсутствовать вместе.
    # Половина пары означает повреждённое signing state; молча создавать другую
    # половину опасно, потому что VM уже доверяет старому сертификату.
    $pfxExists = Test-Path -LiteralPath $localPfxPath
    $passwordExists = Test-Path -LiteralPath $localPasswordPath
    if ($pfxExists -ne $passwordExists) {
        throw "Local signing state is incomplete. Keep or remove both files under $localSigningRoot."
    }
    if (-not $pfxExists) {
        # Первый запуск создаёт постоянный development certificate локально в
        # проекте. В системное certificate store сборочной машины он не импортируется.
        New-Item -ItemType Directory -Path $localSigningRoot -Force | Out-Null
        # 256 случайных бит превращаются в пароль PFX. Это не пароль пользователя.
        $random = [byte[]]::new(32)
        $randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $randomGenerator.GetBytes($random) } finally { $randomGenerator.Dispose() }
        $password = [Convert]::ToBase64String($random)
        # RSA-3072 достаточен для development code signing и поддерживается
        # Windows 11/SignTool без дополнительных провайдеров.
        $rsa = [System.Security.Cryptography.RSA]::Create(3072)
        try {
            # CertificateRequest формирует self-signed сертификат с тем же
            # Subject, который записан как Publisher во всех manifests.
            $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
                $publisher,
                $rsa,
                [System.Security.Cryptography.HashAlgorithmName]::SHA256,
                [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
            # Ограничиваем назначение сертификата только подписью кода.
            $oids = [System.Security.Cryptography.OidCollection]::new()
            [void]$oids.Add([System.Security.Cryptography.Oid]::new($signingEku))
            $request.CertificateExtensions.Add(
                [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($oids, $false))
            # Это end-entity certificate, а не центр сертификации.
            $request.CertificateExtensions.Add(
                [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
            $request.CertificateExtensions.Add(
                [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
                    [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,
                    $true))
            # Пять минут назад компенсируют небольшую рассинхронизацию часов; два
            # года уменьшают частоту переустановки сертификата на тестовой VM.
            $created = $request.CreateSelfSigned(
                [DateTimeOffset]::Now.AddMinutes(-5),
                [DateTimeOffset]::Now.AddYears(2))
            try {
                # Экспортируем сертификат вместе с закрытым ключом в PFX.
                [System.IO.File]::WriteAllBytes(
                    $localPfxPath,
                    $created.Export(
                        [System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx,
                        $password))
            } finally { $created.Dispose() }
        } finally { $rsa.Dispose() }
        # Пароль записывается только после успешного создания PFX.
        [System.IO.File]::WriteAllText($localPasswordPath, $password)
        Write-Host "Created project-local development signing certificate: $localPfxPath"
    }
    # При обычной повторной сборке всегда читается та же сохранённая пара.
    $storedPassword = [System.IO.File]::ReadAllText($localPasswordPath).Trim()
    # MachineKeySet избегает временного user-profile key container; Exportable
    # нужен ниже для получения RSA private key при подписи catalog.json.
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::MachineKeySet
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        [System.IO.File]::ReadAllBytes($localPfxPath), $storedPassword, $flags)
    # Возвращаем сертификат и пароль одним объектом, чтобы вызывающий код не
    # перечитывал и не рассинхронизировал signing state.
    return [pscustomobject]@{ Certificate = $certificate; Password = $storedPassword }
}

# Проверяет именно доступ внешнего SignTool к PFX до сборки Python/Cython.
# Обычный Test-Path здесь недостаточен: ограниченная среда может разрешать
# PowerShell читать файл и одновременно скрывать его от дочернего native process.
function Assert-NativeSignToolAccess {
    # Для настоящей проверки подписываем только временную копию Python launcher.
    # Исходный python.exe и выдаваемые artifacts эта операция не изменяет.
    New-Item -ItemType Directory -Path $workRoot -Force | Out-Null
    $probePath = Join-Path $workRoot "signtool-access-probe.exe"
    Copy-Item -LiteralPath $pythonPath -Destination $probePath -Force
    try {
        [array]$signOutput = @(
            & $script:signTool sign /fd SHA256 /f $localPfxPath `
                /p $script:signingPassword $probePath 2>&1 |
                ForEach-Object { $_.ToString() }
        )
        $signExitCode = $LASTEXITCODE
        if ($signExitCode -ne 0) {
            foreach ($line in $signOutput) { Write-Host $line }
            throw (
                "SignTool preflight failed with exit code $signExitCode. " +
                "The PFX exists and is readable by PowerShell, but the external " +
                "SignTool process could not complete a test signature. Run " +
                "Build-ModelPackageSet.ps1 with unrestricted/external-tool access; " +
                "do not delete or regenerate the project signing certificate."
            )
        }

        # Статус цепочки на build host может быть UnknownError, пока self-signed
        # CER не добавлен в TrustedPeople. Для preflight важно, что подпись есть и
        # выполнена тем же сертификатом, которым затем подписывается Manager MSIX.
        $probeSignature = Get-AuthenticodeSignature -LiteralPath $probePath
        if ($null -eq $probeSignature.SignerCertificate -or
            $probeSignature.SignerCertificate.Thumbprint -ne $script:certificate.Thumbprint) {
            throw "SignTool preflight produced no signature from the project certificate."
        }
        Write-Host "SignTool/PFX native access preflight PASS."
    } finally {
        # Удаляется только созданная выше временная копия внутри version workRoot.
        Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
    }
}

# Подписывает один готовый MSIX постоянным project-local сертификатом.
function Sign-PackageWithProjectCertificate {
    # PackagePath — единственный уже созданный MSIX, который надо подписать.
    param([Parameter(Mandatory = $true)][string]$PackagePath)

    # SignTool умеет читать проектный PFX напрямую. Импортировать private key в
    # CurrentUser\My не требуется: это создавало нестабильность в sandbox и
    # загрязняло системное хранилище сертификатов сборочного компьютера.
    # /fd SHA256 задаёт digest содержимого MSIX; /f и /p указывают PFX и пароль.
    & $script:signTool sign /fd SHA256 /f $localPfxPath /p $script:signingPassword $PackagePath |
        ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw "Package signing failed with exit code $LASTEXITCODE."
    }
}

# Компилирует, подписывает и проверяет единственный Manager MSIX.
function Build-Package {
    param(
        # Короткое имя каталога и диагностик: Manager, XP750, XP960, R3000.
        [Parameter(Mandatory = $true)][string]$Name,
        # Сгенерированные из PrinterRegistry capabilities, PDC и PDR.
        [Parameter(Mandatory = $true)][string]$AssetsRoot,
        # Identity из catalog, с которым после сборки сравнивается AppxManifest.
        [Parameter(Mandatory = $true)][string]$ExpectedIdentity
    )

    # Отдельные bin/obj для этого пакета предотвращают попадание файлов одного
    # model module в следующий model module при четырёх сборках одного csproj.
    $packageBuildRoot = Join-Path $buildRoot $Name
    # Отдельная ветка финальных artifacts этого пакета.
    $packageArtifactRoot = Join-Path $artifactRoot $Name
    # Все свойства передаются одному вызову MSBuild. Важные причины:
    # - Rebuild исключает использование stale outputs;
    # - только x64, потому что продукт и Cython runtime x64-only;
    # - GeneratedAssetsRoot подменяет только registry-derived assets;
    # - PackageBuildVersion меняет Identity.Version в технической копии manifest внутри obj;
    # - подписание отключено в MSBuild и выполняется одинаково нашим SignTool;
    # - bundle не нужен: внутри нет x86/ARM вариантов;
    # - telemetry/appinstaller отключены, чтобы не раздувать тестовый ModuleSet.
    $arguments = @(
        # Какой csproj строить.
        $projectPath,
        # Восстановить NuGet references, если obj/project.assets.json отсутствует.
        "/restore",
        # Полная пересборка этой изолированной роли.
        "/t:Rebuild",
        # Пользовательский пакет: оптимизированная сборка без DEBUG/PDB.
        "/p:Configuration=Release",
        # Единственная поддерживаемая архитектура приложения.
        "/p:Platform=x64",
        # Явный x64 runtime identifier для UWP/.NET dependency resolution.
        "/p:RuntimeIdentifier=win10-x64-aot",
        # Каталог только со сгенерированными assets; manifest всегда берётся из проекта.
        "/p:GeneratedAssetsRoot=$AssetsRoot",
        # Версия подставляется в obj-копию единственного статического manifest.
        "/p:PackageBuildVersion=$Version",
        # Собственный bin роли; завершающий backslash требуется MSBuild paths.
        "/p:OutputPath=$(Join-Path $packageBuildRoot 'bin')\",
        # Собственный obj, включая PRI и техническую versioned-копию manifest.
        "/p:IntermediateOutputPath=$(Join-Path $packageBuildRoot 'obj')\",
        # Куда AppX targets должны положить готовый MSIX и Dependencies.
        "/p:AppxPackageDir=$packageArtifactRoot\",
        # Сначала создаём unsigned MSIX, затем подписываем единообразно SignTool.
        "/p:AppxPackageSigningEnabled=false",
        # Диагностический .appxsym не нужен для пользовательского ModuleSet.
        "/p:AppxSymbolPackageEnabled=false",
        # Пакет предназначен для локального sideload на тестовую VM.
        "/p:UapAppxPackageBuildMode=SideloadOnly",
        # Распакованный test layout нужен только Manager installer workflow.
        "/p:AppxTestLayoutEnabled=true",
        # Не добавлять TelemetryDependencies/LogSideloadingTelemetry.ps1.
        "/p:AppxLogTelemetryFromSideloadingScript=false",
        # Bundle бессмысленен при единственной x64 архитектуре.
        "/p:AppxBundle=Never",
        "/p:AppxBundlePlatforms=x64",
        # Не публиковать ARM/x86 framework dependency branches.
        "/p:AppxExcludeArmFrameworkSdkPackagesFromLayout=true",
        "/p:AppxExcludeX86FrameworkSdkPackagesFromLayout=true",
        # AppInstaller feed/update URL пока не является частью продукта.
        "/p:GenerateAppInstallerFile=false",
        # Минимальный, но достаточный диагностический вывод MSBuild.
        "/verbosity:minimal",
        "/nologo"
    )
    # Массив найденных MSIX всегда нормализуется через @(), чтобы .Count работал
    # одинаково при нуле, одном или нескольких результатах PowerShell pipeline.
    [array]$packages = @()
    # Новый пакет по умолчанию надо подписать. Resume может обнаружить, что это
    # уже было сделано до более позднего сбоя общего ModuleSet.
    $requiresSigning = $true
    if (Test-Path -LiteralPath $packageArtifactRoot) {
        $packages = @(Get-ChildItem -LiteralPath $packageArtifactRoot -Recurse -File -Filter "*.msix")
    }
    if ($Resume -and $packages.Count -eq 1) {
        Write-Host "Reusing existing $Name package from the same resumable build."
        # Предыдущий запуск мог успеть создать MSIX, но оборваться до SignTool.
        # Поэтому существование файла ещё не доказывает готовность к установке.
        $existingSignature = Get-AuthenticodeSignature -FilePath $packages[0].FullName
        if ($null -ne $existingSignature.SignerCertificate) {
            $requiresSigning = $false
        } else {
            Write-Host "Existing $Name package is unsigned; signing it now."
        }
    } else {
        # Без корректного Resume наличие пакета неоднозначно: запрещаем
        # автоматическую перезапись или выбор одного файла из нескольких.
        if ($packages.Count -ne 0) {
            throw "Cannot resume ${Name}: expected zero or one existing MSIX, found $($packages.Count)."
        }
        # @arguments раскрывается в отдельные аргументы процесса MSBuild. Вывод
        # оставляем пользователю: при ошибке он содержит исходный target/file/line.
        & $script:msbuild @arguments | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            throw "$Name package build failed with exit code $LASTEXITCODE."
        }

        # После успешного MSBuild допускается ровно один MSIX конкретной роли.
        $packages = @(Get-ChildItem -LiteralPath $packageArtifactRoot -Recurse -File -Filter "*.msix")
        if ($packages.Count -ne 1) {
            throw "Expected one $Name MSIX, found $($packages.Count)."
        }
    }
    if ($requiresSigning) {
        Sign-PackageWithProjectCertificate $packages[0].FullName
    }
    # MSIX является ZIP-контейнером. Открываем его read-only и проверяем не имя
    # файла на диске, а фактический manifest и payload внутри подписанного пакета.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($packages[0].FullName)
    try {
        # AppxManifest.xml обязан существовать в корне любого корректного MSIX.
        $manifestEntry = $archive.GetEntry("AppxManifest.xml")
        if (-not $manifestEntry) { throw "$Name MSIX has no AppxManifest.xml." }
        $reader = [System.IO.StreamReader]::new($manifestEntry.Open())
        try { [xml]$manifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
        # Главная защита от кросс-загрязнения: XP750 не может случайно получить
        # identity XP960, даже если MSBuild завершился без собственной ошибки.
        if ([string]$manifest.Package.Identity.Name -ne $ExpectedIdentity) {
            throw "$Name identity mismatch: $($manifest.Package.Identity.Name)."
        }
        if ([string]$manifest.Package.Identity.Version -ne $Version) {
            throw "$Name version mismatch: $($manifest.Package.Identity.Version)."
        }
        if (-not $archive.GetEntry("Assets/PdrModels/index.json")) {
            throw "Manager package has no model-localized PDR index."
        }
        # VirtualPrinterManager installs queues against this one PSA app.
        # A second Application entry coincided with PrinterInstallationFailed
        # on 1.0.0.160, while the checked PDC/PDR files were unchanged.
        # OnLaunched owns the Manager image; OnActivated shows only the ring.
        $allApps = @($manifest.SelectNodes(
            "//*[local-name()='Applications']/*[local-name()='Application']"))
        if ($allApps.Count -ne 1 -or $allApps[0].GetAttribute('Id') -ne 'App') {
            throw "Manager package must contain exactly one PSA Application with Id App."
        }
        $managerApp = $allApps[0]
        $nativeSplash = @($managerApp.SelectNodes(
            ".//*[local-name()='VisualElements']/*[local-name()='SplashScreen']"))
        if ($nativeSplash.Count -ne 0) {
            throw "The shared PSA Application cannot show a native image only for Manager launch."
        }
        $settingsExtension = @($managerApp.SelectNodes(
            ".//*[local-name()='Extension' and @Category='windows.printSupportSettingsUI']"))
        $workflowExtension = @($managerApp.SelectNodes(
            ".//*[local-name()='Extension' and @Category='windows.printSupportVirtualPrinterWorkflow']"))
        if ($settingsExtension.Count -ne 1 -or $workflowExtension.Count -ne 1) {
            throw "The PSA Application must own Settings UI and Virtual Printer workflow."
        }
        if (-not $archive.GetEntry("Assets/SplashScreen.png")) {
            throw "Manager package has no image for the launch-only ExtendedSplash."
        }
        $publisherCacheNodes = @($manifest.SelectNodes(
            "//*[local-name()='Extension' and @Category='windows.publisherCacheFolders']/*[local-name()='PublisherCacheFolders']/*[local-name()='Folder' and @Name='pyToneRIP.Shared']"))
        if ($publisherCacheNodes.Count -ne 1) {
            throw "Manager package must declare the pyToneRIP.Shared publisher cache folder."
        }
        # Windows deployment performs this semantic check only at installation:
        # an in-process AppService (no extension EntryPoint) cannot belong to an
        # Application marked SupportsMultipleInstances=true. MSBuild/MakeAppx can
        # still produce and sign such an invalid package, so fail the factory here.
        $applicationNodes = @($manifest.SelectNodes(
            "//*[local-name()='Applications']/*[local-name()='Application']"))
        foreach ($applicationNode in $applicationNodes) {
            $appServiceNodes = @($applicationNode.SelectNodes(
                ".//*[local-name()='Extension' and @Category='windows.appService']"))
            $multiInstanceValue = $applicationNode.GetAttribute(
                    "SupportsMultipleInstances",
                    "http://schemas.microsoft.com/appx/manifest/uap/windows10/10")
            if ($multiInstanceValue -eq "true") {
                foreach ($appServiceNode in $appServiceNodes) {
                    if (-not $appServiceNode.HasAttribute("EntryPoint")) {
                        throw "$Name manifest is deployment-invalid: an in-process AppService cannot be combined with SupportsMultipleInstances=true."
                    }
                }
            }
        }
        if (-not $archive.GetEntry("pyToneRIP.QueueHelper.exe")) {
            throw "Manager package has no QueueHelper."
        }
        if (-not $archive.GetEntry("Runtime/pyToneRIP.Runtime.exe")) {
            throw "Manager package has no shared Python runtime."
        }
    } finally {
        # Освобождаем file handle, иначе последующая подпись/копирование/очистка
        # на Windows может получить Access Denied.
        $archive.Dispose()
    }

    # Возвращаем FileInfo, чтобы вызывающий код добавил путь и SHA-256 в catalog.
    return $packages[0]
}

# Проверяет принадлежность файла ModuleSet и возвращает переносимый relative path.
function Get-ArtifactRelativePath {
    # Catalog должен быть переносимым между C:\dev-test, Y:\ и любым другим
    # каталогом VM, поэтому в нём хранятся относительные, а не абсолютные пути.
    param([Parameter(Mandatory = $true)][string]$Path)
    # Завершающий backslash не позволяет пути с общим текстовым префиксом
    # (например artifacts-evil) пройти проверку как дочернему.
    $root = [System.IO.Path]::GetFullPath($artifactRoot).TrimEnd('\') + '\'
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    # Никогда не публикуем в catalog файл за пределами текущего ModuleSet.
    if (-not $fullPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Package output is outside the artifact set: $fullPath"
    }
    return $fullPath.Substring($root.Length)
}

# Возвращает catalog entries x64 framework dependencies одного MSIX.
function Get-PackageDependencies {
    # UWP framework dependencies MSBuild кладёт рядом с конкретным MSIX в
    # Dependencies\x64. Manager installer должен знать их точные файлы и хеши.
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$Package)
    $dependencyRoot = Join-Path $Package.Directory.FullName "Dependencies\x64"
    # Пустой список корректен, если все необходимые framework packages уже
    # включены иначе или конкретная роль не породила dependencies.
    if (-not (Test-Path -LiteralPath $dependencyRoot)) {
        return @()
    }
    # Стабильная сортировка даёт deterministic catalog. Для каждого APPX
    # записываем переносимый путь и SHA-256, проверяемый перед установкой.
    return @(
        Get-ChildItem -LiteralPath $dependencyRoot -File -Filter "*.appx" |
            Sort-Object Name |
            ForEach-Object {
                [pscustomobject]@{
                    package_file = Get-ArtifactRelativePath $_.FullName
                    package_sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
                }
            }
    )
}

# До начала дорогой работы проверяем обязательное проектное окружение.
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project-local Python environment was not found: $pythonPath"
}
# Нельзя молча затереть уже выданную версию. Для продолжения оборванной именно
# этой сборки пользователь должен явно передать -Resume.
if ((Test-Path -LiteralPath $artifactRoot) -and -not $Resume) {
    throw "Artifact set already exists; refusing to overwrite it: $artifactRoot"
}
if ($Resume -and (Test-Path -LiteralPath $readyMarkerPath -PathType Leaf)) {
    throw "Artifact set is already complete and immutable: $artifactRoot"
}

# Сертификат, MSBuild и SignTool проверяются до дорогой сборки runtime. Важно
# запускать весь Package Factory с внешним доступом с первой попытки: sandbox
# может показывать PFX самому PowerShell, но скрывать его от SignTool.exe.
$localSigning = Get-LocalDevelopmentCertificate
$script:certificate = $localSigning.Certificate
$script:signingPassword = $localSigning.Password
$script:msbuild = Find-MSBuild
$script:signTool = Find-SignTool
Assert-NativeSignToolAccess
if ($SigningPreflightOnly) {
    Write-Host "Signing preflight completed; runtime and MSIX were not built."
    return
}

if (-not $SkipPythonRuntimeBuild) {
    # Полная сборка создаёт свежий Cython .pyd и PyInstaller one-folder runtime.
    # Это выполняется один раз: model packages не содержат и не дублируют runtime.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runtimeBuild
    if ($LASTEXITCODE -ne 0) { throw "Python runtime build failed with exit code $LASTEXITCODE." }
} elseif (-not (Test-Path -LiteralPath (Join-Path $scriptRoot "runtime\dist\pyToneRIP.Runtime\pyToneRIP.Runtime.exe"))) {
    # Skip разрешён только при наличии уже проверяемого executable. Иначе пакет
    # мог бы успешно скомпилироваться, но не уметь запускать backend на VM.
    throw "The verified shared Python runtime is missing."
}

# Python registry является единственным источником моделей. Generator создаёт
# общий capabilities JSON и отдельный PDC/PDR для каждой модели в generated/Assets.
# Статический manifest при этом не генерируется. Отдельные model MSIX не нужны:
# VirtualPrinterManager связывает выбранный PDC с очередью во время Add.
Write-Host "Generating package assets for $Version; this step may take several minutes..."
& $pythonPath -X utf8 -m xp750_rip psa-generate-package-assets --output-dir $generatedRoot --version $Version
$assetGeneratorExitCode = $LASTEXITCODE
if ($assetGeneratorExitCode -ne 0) {
    throw ("Package asset generation failed: Python exit code {0} (0x{1}); output={2}. See the Python error above." -f
        $assetGeneratorExitCode, ([int]$assetGeneratorExitCode).ToString("X8"), $generatedRoot)
}
if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) {
    throw "Package asset generator exited successfully but did not create the catalog: $catalogPath"
}
Write-Host "Package assets generated: $catalogPath"

# Artifact root создаётся только после всех ранних preflight checks.
New-Item -ItemType Directory -Path $artifactRoot -Force | Out-Null
# На VM нужен только публичный CER: им разрешают доверять подписи. Закрытый ключ
# PFX никогда не публикуется в artifacts.
[System.IO.File]::WriteAllBytes(
    (Join-Path $artifactRoot "pyToneRIP-TestSigning.cer"),
    $script:certificate.Export(
        [System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
# Читаем generated catalog как изменяемый PowerShell object. Он описывает
# модели/PDC, но единственным устанавливаемым пакетом является Manager.
$catalog = Get-Content -LiteralPath $catalogPath -Raw | ConvertFrom-Json
# Manager содержит GUI, все model PDC, workflow task, helper и общий runtime.
$managerPackage = Build-Package -Name "Manager" `
    -AssetsRoot (Join-Path $generatedRoot "Assets") `
    -ExpectedIdentity ([string]$catalog.core.package_identity)
# Записываем относительный путь реально найденного MSIX.
$catalog.core | Add-Member -NotePropertyName package_file `
    -NotePropertyValue (Get-ArtifactRelativePath $managerPackage.FullName)
# SHA-256 защищает от повреждения/подмены файла после подписи catalog.
$catalog.core | Add-Member -NotePropertyName package_sha256 `
    -NotePropertyValue ((Get-FileHash -LiteralPath $managerPackage.FullName -Algorithm SHA256).Hash)
# Framework dependencies также входят в проверяемый catalog.
$catalog.core | Add-Member -NotePropertyName dependencies `
    -NotePropertyValue @(Get-PackageDependencies $managerPackage)

# PDC уже находятся внутри подписанного Manager MSIX. Catalog хранит их
# package-relative paths и hashes для независимого аудита сборки.
# Depth 8 достаточен для core/models/dependencies и не обрезает вложенные поля.
$catalogJson = $catalog | ConvertTo-Json -Depth 8
# Windows PowerShell 5.1 Set-Content -Encoding UTF8 добавляет BOM. Catalog
# подписывается byte-for-byte и читается UWP JSON parser, поэтому явно создаём
# канонический UTF-8 без BOM и с одним завершающим переводом строки.
[System.IO.File]::WriteAllText(
    $publishedCatalogPath,
    $catalogJson + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false))
# Подписываем именно записанные на диск bytes, а не строку в памяти.
$catalogBytes = [System.IO.File]::ReadAllBytes($publishedCatalogPath)
# Из X509 извлекается RSA private key того же сертификата, которым подписаны MSIX.
$rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey(
    $script:certificate)
try {
    # PKCS#1 v1.5 + SHA-256 соответствует проверке PublishedModuleCatalogService.
    $signature = $rsa.SignData(
        $catalogBytes,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
} finally {
    # Private-key handle освобождается сразу после подписи catalog.
    $rsa.Dispose()
}
# Base64 позволяет хранить бинарную RSA signature в простом ASCII-файле.
[Convert]::ToBase64String($signature) | Set-Content `
    -LiteralPath (Join-Path $artifactRoot "catalog.sig") -Encoding Ascii
# Эти сценарии копируются внутрь переносимого ModuleSet, чтобы VM не зависела от
# исходного репозитория для установки, удаления и read-only package audit.
Copy-Item -LiteralPath (Join-Path $scriptRoot "Install-ModuleSet-Manager.ps1") `
    -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $scriptRoot "Uninstall-pyToneRIP.ps1") `
    -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $scriptRoot "Clear-PyToneRipPrintState.ps1") `
    -Destination $artifactRoot -Force
Copy-Item -LiteralPath (Join-Path $scriptRoot "Test-ModulePackageSet.ps1") `
    -Destination $artifactRoot -Force
# Если для этой версии подготовлена точная VM-инструкция, кладём её рядом с
# installer. Отсутствие документа не ломает общую фабрику будущих версий.
$vmGuide = Join-Path $scriptRoot ("TEST_" + $Version + "_VM_RU.md")
if (Test-Path -LiteralPath $vmGuide -PathType Leaf) {
    Copy-Item -LiteralPath $vmGuide -Destination $artifactRoot -Force
}
# Маркер создаётся последним из содержательных файлов и связывает конкретные
# catalog/MSIX по SHA-256. Если следующий независимый аудит не пройдёт, маркер
# обязательно удаляется, поэтому частичный artifact невозможно принять за релиз.
$readyMarker = [ordered]@{
    schema = 'pytone-rip.package-ready'
    version = $Version
    catalog_sha256 = (Get-FileHash -LiteralPath $publishedCatalogPath -Algorithm SHA256).Hash
    manager_sha256 = [string]$catalog.core.package_sha256
    completed_utc = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json
[System.IO.File]::WriteAllText(
    $readyMarkerPath,
    $readyMarker + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false))
# Финальный аудит запускается как отдельный PowerShell process. Это важно:
# проверка не наследует случайные функции/переменные текущего build process и
# анализирует ровно тот переносимый каталог, который получит пользователь.
& powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $scriptRoot "Test-ModulePackageSet.ps1") `
    -ModuleSetRoot $artifactRoot
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $readyMarkerPath -Force -ErrorAction SilentlyContinue
    throw "Final ModuleSet audit failed with exit code $LASTEXITCODE."
}
# Закрываем X509 object после завершения всех MSIX/catalog подписей.
$script:certificate.Dispose()
# Единственное финальное PASS-сообщение означает: generation, compile, signing,
# per-package boundary audit и final ModuleSet audit все успешно завершены.
Write-Host "Package Factory PASS: $publishedCatalogPath"
