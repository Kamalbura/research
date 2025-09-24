param([string]$suite = "cs-kyber768-aesgcm-dilithium3")
$map = @{
  "cs-kyber512-aesgcm-dilithium2"      = "gcs/wrappers/gcs_kyber_512.py"
  "cs-kyber768-aesgcm-dilithium3"      = "gcs/wrappers/gcs_kyber_768.py"
  "cs-kyber1024-aesgcm-dilithium5"     = "gcs/wrappers/gcs_kyber_1024.py"
  "cs-kyber768-aesgcm-falcon512"       = "gcs/wrappers/gcs_falcon512.py"
  "cs-kyber1024-aesgcm-falcon1024"     = "gcs/wrappers/gcs_falcon1024.py"
  "cs-kyber512-aesgcm-sphincs128f_sha2"= "gcs/wrappers/gcs_sphincs_sha2_128f.py"
  "cs-kyber1024-aesgcm-sphincs256f_sha2"= "gcs/wrappers/gcs_sphincs_sha2_256f.py"
}
if (-not $map.ContainsKey($suite)) { Write-Error "Unknown suite $suite"; exit 2 }
python $map[$suite]
