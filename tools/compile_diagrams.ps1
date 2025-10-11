# PowerShell script to compile all Mermaid diagrams in docs/diagrams to SVGs
$diagramDir = "docs\diagrams"
$outputDir = "tools\images"

if (-not (Test-Path $outputDir)) {
    Write-Host "Creating output directory: $outputDir"
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

Get-ChildItem -Path $diagramDir -Filter *.md | ForEach-Object {
    $inputFile = $_.FullName
    $outputFile = Join-Path $outputDir ($_.BaseName + ".svg")
    Write-Host "Compiling: $inputFile -> $outputFile"
    & mmdc -i $inputFile -o $outputFile
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Error compiling $($_.Name)"
    }
}