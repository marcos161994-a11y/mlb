param(
    [string]$Secret = $env:CRON_SECRET,
    [string]$MemoriaPath = "$PSScriptRoot\memoria_auditoria.json"
)

$base = "https://mlb-1-en7i.onrender.com"

if (-not (Test-Path $MemoriaPath)) {
    Write-Host "No existe: $MemoriaPath" -ForegroundColor Red
    exit 1
}

if (-not $Secret) {
    $Secret = Read-Host "Pega tu CRON_SECRET de Render"
}
if (-not $Secret) {
    Write-Host "Sin secret no se puede subir memoria." -ForegroundColor Yellow
    exit 1
}

$json = Get-Content -Raw -Encoding UTF8 $MemoriaPath
$data = $json | ConvertFrom-Json
Write-Host "Subiendo memoria: dia=$($data.dia_actual) capital=$($data.capital) ..." -ForegroundColor Cyan

try {
    $url = "$base/api/subir-memoria?secret=$([uri]::EscapeDataString($Secret))"
    $r = Invoke-WebRequest -Uri $url -Method POST -Body $json -ContentType "application/json; charset=utf-8" -TimeoutSec 120 -UseBasicParsing
    $j = $r.Content | ConvertFrom-Json
    if ($j.ok) {
        Write-Host "OK nube: dia=$($j.dia_actual) capital=$($j.capital) dias_guardados=$($j.dias)" -ForegroundColor Green
    } else {
        Write-Host "Error: $($j | ConvertTo-Json)" -ForegroundColor Red
    }
} catch {
    Write-Host "Fallo: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
