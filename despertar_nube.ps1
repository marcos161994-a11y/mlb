param(
    [string]$Secret = $env:CRON_SECRET
)

$base = "https://mlb-1-en7i.onrender.com"
Write-Host "Comprobando salud..." -ForegroundColor Cyan
try {
    $h = Invoke-WebRequest -Uri "$base/api/health" -TimeoutSec 90 -UseBasicParsing
    Write-Host "Health: $($h.StatusCode)" -ForegroundColor Green
} catch {
    Write-Host "Health fallo: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $Secret) {
    $Secret = Read-Host "Pega tu CRON_SECRET de Render (Dashboard > Environment)"
}
if (-not $Secret) {
    Write-Host "Sin secret no se puede ejecutar auto-bloqueo (403)." -ForegroundColor Yellow
    exit 1
}

Write-Host "Ejecutando auto-bloqueo + liquidacion (puede tardar 1-2 min)..." -ForegroundColor Cyan
try {
    $url = "$base/api/auto-bloqueo-externo?secret=$([uri]::EscapeDataString($Secret))"
    $r = Invoke-WebRequest -Uri $url -TimeoutSec 180 -UseBasicParsing
    $j = $r.Content | ConvertFrom-Json
    if ($j.ok) {
        Write-Host "OK dia=$($j.dia_actual) capital=$($j.capital) fecha=$($j.fecha_hoy)" -ForegroundColor Green
    } else {
        Write-Host "Error: $($j.error)" -ForegroundColor Red
    }
} catch {
    Write-Host "Auto-bloqueo fallo: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
