$CONFIG = "M:\Working\Apple\apple_monitor\apple_monitor_config.py"
$ZONE   = "us-central1-a"
$DEST   = "dphm57@apple-monitor:/home/dphm57/apple_monitor/apple_monitor_config.py"

Write-Host "  Syncing config to VM..."
gcloud compute scp $CONFIG $DEST --zone=$ZONE --quiet 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  Config synced to VM OK"
} else {
    Write-Host "  [WARN] Sync to VM failed (VM may be down)"
}
