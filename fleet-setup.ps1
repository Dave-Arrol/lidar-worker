# fleet-setup.ps1 - create everything the Arrol fleet sync needs on AWS:
#   1) Secrets Manager secret  arrol/fleet        (Komatsu key + Deere client creds)
#   2) IAM task role           arrol-fleet-task-role   (S3 machine-files/ access)
#      + secrets-read inline policy on ecsTaskExecutionRole
#   3) ECR repository          arrol-fleet-sync
#   4) Batch compute env       arrol-fleet-ce     (Fargate, maxvCpus=1 - its own lane,
#      so a fleet poll never queues behind a 16-vCPU LiDAR job)
#      Batch job queue         arrol-fleet-queue
#      Batch job definition    arrol-fleet-sync   (0.5 vCPU / 1 GB)
#   5) EventBridge Scheduler   arrol-fleet-sync-15min  (rate 15 minutes -> SubmitJob)
#
# Run in an elevated PowerShell with ADMIN AWS credentials (it creates IAM roles).
# Safe to re-run: existing resources are skipped or updated in place.
#
# It reuses your existing infrastructure choices automatically:
#   - subnets + security group are read from the arrol-lidar-ce compute environment
#   - execution role + SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are read from the
#     arrol-lidar-worker job definition (values or secret ARNs, whichever it uses)
#
# You will be prompted for: KOMATSU_API_KEY, DEERE_CLIENT_ID, DEERE_CLIENT_SECRET.
#
# AFTER this script: build + push the image (commands printed at the end), then
# submit a test job with:
#   aws batch submit-job --job-name fleet-test --job-queue arrol-fleet-queue --job-definition arrol-fleet-sync --region eu-west-2

param(
  [string]$Region      = "eu-west-2",
  [string]$Account     = "994114819090",
  [string]$SecretName  = "arrol/fleet",
  [string]$TaskRole    = "arrol-fleet-task-role",
  [string]$SchedRole   = "arrol-fleet-scheduler-role",
  [string]$EcrRepo     = "arrol-fleet-sync",
  [string]$CeName      = "arrol-fleet-ce",
  [string]$QueueName   = "arrol-fleet-queue",
  [string]$JobDefName  = "arrol-fleet-sync",
  [string]$ScheduleName= "arrol-fleet-sync-15min",
  [string]$LidarCe     = "arrol-lidar-ce",
  [string]$LidarJobDef = "arrol-lidar-worker",
  [string]$Bucket      = "arrol-lidar"
)

$ErrorActionPreference = "Stop"

function Write-JsonFile([string]$Path, [string]$Json) {
  [System.IO.File]::WriteAllText($Path, $Json, [System.Text.Encoding]::ASCII)
}

Write-Host ""
Write-Host "=== Arrol fleet sync setup ($Region / $Account) ===" -ForegroundColor Cyan

# ---- 0) Collect credentials -------------------------------------------------
$komatsuKey  = Read-Host "Komatsu API key (X-Api-Key)"
$deereId     = Read-Host "John Deere client id"
$deereSecret = Read-Host "John Deere client secret"
if (-not $komatsuKey)  { throw "Komatsu API key is required." }
if (-not $deereId -or -not $deereSecret) { throw "Deere client id + secret are required." }

# ---- 1) Secrets Manager: arrol/fleet ---------------------------------------
Write-Host "1) Secret '$SecretName'..."
$secretObj = @{
  KOMATSU_API_KEY     = $komatsuKey
  DEERE_CLIENT_ID     = $deereId
  DEERE_CLIENT_SECRET = $deereSecret
} | ConvertTo-Json -Compress
$secretFile = Join-Path $env:TEMP "arrol-fleet-secret.json"
Write-JsonFile $secretFile $secretObj

$secretArn = $null
try {
  $existing = aws secretsmanager describe-secret --secret-id $SecretName --region $Region --output json 2>$null | ConvertFrom-Json
  if ($existing) { $secretArn = $existing.ARN }
} catch {}
if ($secretArn) {
  aws secretsmanager put-secret-value --secret-id $SecretName --secret-string "file://$secretFile" --region $Region | Out-Null
  Write-Host "   updated existing secret."
} else {
  $created = aws secretsmanager create-secret --name $SecretName --secret-string "file://$secretFile" --region $Region --output json | ConvertFrom-Json
  $secretArn = $created.ARN
  Write-Host "   created secret."
}
Remove-Item $secretFile -Force
Write-Host "   ARN: $secretArn"

# ---- 2) Read reusable settings from the LiDAR setup -------------------------
Write-Host "2) Reading network + Supabase settings from existing LiDAR setup..."
$lidarCeJson = aws batch describe-compute-environments --compute-environments $LidarCe --region $Region --output json | ConvertFrom-Json
if (-not $lidarCeJson.computeEnvironments) { throw "Compute environment '$LidarCe' not found - is the region right?" }
$subnets = $lidarCeJson.computeEnvironments[0].computeResources.subnets
$sgs     = $lidarCeJson.computeEnvironments[0].computeResources.securityGroupIds
Write-Host "   subnets: $($subnets -join ', ')"
Write-Host "   security groups: $($sgs -join ', ')"

$lidarDefJson = aws batch describe-job-definitions --job-definition-name $LidarJobDef --status ACTIVE --region $Region --output json | ConvertFrom-Json
if (-not $lidarDefJson.jobDefinitions) { throw "Job definition '$LidarJobDef' not found." }
$lidarDef = $lidarDefJson.jobDefinitions | Sort-Object revision -Descending | Select-Object -First 1
$execRole = $lidarDef.containerProperties.executionRoleArn
Write-Host "   execution role: $execRole"

$lidarEnv     = @($lidarDef.containerProperties.environment)
$lidarSecrets = @($lidarDef.containerProperties.secrets)

function Get-EnvValue([string]$Name) {
  $e = $lidarEnv | Where-Object { $_.name -eq $Name } | Select-Object -First 1
  if ($e) { return $e.value } else { return $null }
}
function Get-SecretRef([string]$Name) {
  $s = $lidarSecrets | Where-Object { $_.name -eq $Name } | Select-Object -First 1
  if ($s) { return $s.valueFrom } else { return $null }
}

$supabaseUrl       = Get-EnvValue "SUPABASE_URL"
$supabaseKeyValue  = Get-EnvValue "SUPABASE_SERVICE_ROLE_KEY"
$supabaseKeyRef    = Get-SecretRef "SUPABASE_SERVICE_ROLE_KEY"
if (-not $supabaseUrl) { $supabaseUrl = Read-Host "SUPABASE_URL (not found in the LiDAR job def)" }
if (-not $supabaseKeyValue -and -not $supabaseKeyRef) {
  $supabaseKeyValue = Read-Host "SUPABASE_SERVICE_ROLE_KEY (not found in the LiDAR job def)"
}
Write-Host "   SUPABASE_URL: $supabaseUrl"

# ---- 3) IAM: task role (S3) + secrets access on the execution role ----------
Write-Host "3) IAM roles and policies..."
$ecsTrust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$trustFile = Join-Path $env:TEMP "arrol-fleet-trust.json"
Write-JsonFile $trustFile $ecsTrust

$taskRoleArn = $null
try {
  $r = aws iam get-role --role-name $TaskRole --output json 2>$null | ConvertFrom-Json
  if ($r) { $taskRoleArn = $r.Role.Arn; Write-Host "   task role exists." }
} catch {}
if (-not $taskRoleArn) {
  $r = aws iam create-role --role-name $TaskRole --assume-role-policy-document "file://$trustFile" --output json | ConvertFrom-Json
  $taskRoleArn = $r.Role.Arn
  Write-Host "   created task role."
}

$s3Policy = @"
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":"arn:aws:s3:::$Bucket/machine-files/*"},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::$Bucket","Condition":{"StringLike":{"s3:prefix":"machine-files/*"}}}
]}
"@
$s3PolicyFile = Join-Path $env:TEMP "arrol-fleet-s3.json"
Write-JsonFile $s3PolicyFile $s3Policy
aws iam put-role-policy --role-name $TaskRole --policy-name "arrol-fleet-s3-machine-files" --policy-document "file://$s3PolicyFile" | Out-Null
Write-Host "   S3 policy attached to task role."

# The execution role must be able to read the fleet secret (and any secret the
# reused SUPABASE_SERVICE_ROLE_KEY reference points at is already permitted).
$execRoleName = ($execRole -split "/")[-1]
$secPolicy = "{`"Version`":`"2012-10-17`",`"Statement`":[{`"Effect`":`"Allow`",`"Action`":`"secretsmanager:GetSecretValue`",`"Resource`":`"$secretArn`"}]}"
$secPolicyFile = Join-Path $env:TEMP "arrol-fleet-secrets-policy.json"
Write-JsonFile $secPolicyFile $secPolicy
aws iam put-role-policy --role-name $execRoleName --policy-name "arrol-fleet-secrets" --policy-document "file://$secPolicyFile" | Out-Null
Write-Host "   secrets-read policy attached to $execRoleName."

# ---- 4) ECR repository -------------------------------------------------------
Write-Host "4) ECR repository '$EcrRepo'..."
try {
  aws ecr describe-repositories --repository-names $EcrRepo --region $Region --output json 2>$null | Out-Null
  Write-Host "   already exists."
} catch {
  aws ecr create-repository --repository-name $EcrRepo --region $Region | Out-Null
  Write-Host "   created."
}

# ---- 5) Batch: compute environment, queue, job definition --------------------
Write-Host "5) Batch compute environment '$CeName' (Fargate, maxvCpus=1)..."
$ceExists = $false
try {
  $ce = aws batch describe-compute-environments --compute-environments $CeName --region $Region --output json | ConvertFrom-Json
  if ($ce.computeEnvironments.Count -gt 0) { $ceExists = $true }
} catch {}
if ($ceExists) {
  Write-Host "   already exists - skipping."
} else {
  $subnetsJson = ($subnets | ForEach-Object { '"' + $_ + '"' }) -join ","
  $sgsJson     = ($sgs     | ForEach-Object { '"' + $_ + '"' }) -join ","
  $ceJson = @"
{
  "computeEnvironmentName": "$CeName",
  "type": "MANAGED",
  "state": "ENABLED",
  "computeResources": {
    "type": "FARGATE",
    "maxvCpus": 1,
    "subnets": [$subnetsJson],
    "securityGroupIds": [$sgsJson]
  }
}
"@
  $ceFile = Join-Path $env:TEMP "arrol-fleet-ce.json"
  Write-JsonFile $ceFile $ceJson
  aws batch create-compute-environment --cli-input-json "file://$ceFile" --region $Region | Out-Null
  Write-Host "   created. Waiting for VALID..."
  for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 5
    $desc = aws batch describe-compute-environments --compute-environments $CeName --region $Region --output json | ConvertFrom-Json
    $status = $desc.computeEnvironments[0].status
    if ($status -eq "VALID") { break }
    if ($status -eq "INVALID") { throw "Compute environment INVALID: $($desc.computeEnvironments[0].statusReason)" }
  }
  Write-Host "   VALID."
}

Write-Host "   job queue '$QueueName'..."
$qExists = $false
try {
  $q = aws batch describe-job-queues --job-queues $QueueName --region $Region --output json | ConvertFrom-Json
  if ($q.jobQueues.Count -gt 0) { $qExists = $true }
} catch {}
if ($qExists) {
  Write-Host "   already exists - skipping."
} else {
  aws batch create-job-queue --job-queue-name $QueueName --state ENABLED --priority 1 `
    --compute-environment-order "order=1,computeEnvironment=$CeName" --region $Region | Out-Null
  Write-Host "   created."
}

Write-Host "   job definition '$JobDefName'..."
$image = "$Account.dkr.ecr.$Region.amazonaws.com/${EcrRepo}:latest"

$envEntries = @(
  @{ name = "SUPABASE_URL";     value = $supabaseUrl },
  @{ name = "S3_BUCKET";        value = $Bucket },
  @{ name = "AWS_REGION";       value = $Region },
  @{ name = "KOMATSU_API_BASE"; value = "https://smartforestry.komatsuforest.com:6001/Stanford" },
  @{ name = "DEERE_OAUTH_BASE"; value = "https://signin.johndeere.com/oauth2/aus78tnlaysMraFhC1t7" },
  @{ name = "DEERE_API_BASE";   value = "https://partnerapi.deere.com/platform" }
)
$secretEntries = @(
  @{ name = "KOMATSU_API_KEY";     valueFrom = "${secretArn}:KOMATSU_API_KEY::" },
  @{ name = "DEERE_CLIENT_ID";     valueFrom = "${secretArn}:DEERE_CLIENT_ID::" },
  @{ name = "DEERE_CLIENT_SECRET"; valueFrom = "${secretArn}:DEERE_CLIENT_SECRET::" }
)
if ($supabaseKeyRef) {
  $secretEntries += @{ name = "SUPABASE_SERVICE_ROLE_KEY"; valueFrom = $supabaseKeyRef }
} else {
  $envEntries += @{ name = "SUPABASE_SERVICE_ROLE_KEY"; value = $supabaseKeyValue }
}
$envJson = ($envEntries    | ForEach-Object { '{"name":"' + $_.name + '","value":"' + $_.value + '"}' }) -join ","
$secJson = ($secretEntries | ForEach-Object { '{"name":"' + $_.name + '","valueFrom":"' + $_.valueFrom + '"}' }) -join ","

$jobDefJson = @"
{
  "jobDefinitionName": "$JobDefName",
  "type": "container",
  "platformCapabilities": ["FARGATE"],
  "containerProperties": {
    "image": "$image",
    "command": ["node", "sync.js"],
    "jobRoleArn": "$taskRoleArn",
    "executionRoleArn": "$execRole",
    "resourceRequirements": [
      { "type": "VCPU",   "value": "0.5"  },
      { "type": "MEMORY", "value": "1024" }
    ],
    "environment": [$envJson],
    "secrets": [$secJson],
    "networkConfiguration": { "assignPublicIp": "ENABLED" },
    "fargatePlatformConfiguration": { "platformVersion": "LATEST" }
  },
  "retryStrategy": { "attempts": 1 },
  "timeout": { "attemptDurationSeconds": 1800 }
}
"@
$jobDefFile = Join-Path $env:TEMP "arrol-fleet-jobdef.json"
Write-JsonFile $jobDefFile $jobDefJson
aws batch register-job-definition --cli-input-json "file://$jobDefFile" --region $Region | Out-Null
Remove-Item $jobDefFile -Force
Write-Host "   registered (new revision)."

# ---- 6) EventBridge Scheduler: every 15 minutes ------------------------------
Write-Host "6) EventBridge Scheduler '$ScheduleName' (rate 15 minutes)..."
$schedTrust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
$schedTrustFile = Join-Path $env:TEMP "arrol-fleet-sched-trust.json"
Write-JsonFile $schedTrustFile $schedTrust

$schedRoleArn = $null
try {
  $r = aws iam get-role --role-name $SchedRole --output json 2>$null | ConvertFrom-Json
  if ($r) { $schedRoleArn = $r.Role.Arn }
} catch {}
if (-not $schedRoleArn) {
  $r = aws iam create-role --role-name $SchedRole --assume-role-policy-document "file://$schedTrustFile" --output json | ConvertFrom-Json
  $schedRoleArn = $r.Role.Arn
  Write-Host "   created scheduler role."
}
$queueArn  = "arn:aws:batch:${Region}:${Account}:job-queue/$QueueName"
$jobDefArn = "arn:aws:batch:${Region}:${Account}:job-definition/${JobDefName}"
$submitPolicy = "{`"Version`":`"2012-10-17`",`"Statement`":[{`"Effect`":`"Allow`",`"Action`":`"batch:SubmitJob`",`"Resource`":[`"$queueArn`",`"$jobDefArn`",`"${jobDefArn}:*`"]}]}"
$submitPolicyFile = Join-Path $env:TEMP "arrol-fleet-submit.json"
Write-JsonFile $submitPolicyFile $submitPolicy
aws iam put-role-policy --role-name $SchedRole --policy-name "arrol-fleet-submitjob" --policy-document "file://$submitPolicyFile" | Out-Null

$inputEscaped = '{\"JobName\":\"arrol-fleet-scheduled\",\"JobQueue\":\"' + $QueueName + '\",\"JobDefinition\":\"' + $JobDefName + '\"}'
$scheduleJson = @"
{
  "Name": "$ScheduleName",
  "ScheduleExpression": "rate(15 minutes)",
  "FlexibleTimeWindow": { "Mode": "OFF" },
  "Target": {
    "Arn": "arn:aws:scheduler:::aws-sdk:batch:submitJob",
    "RoleArn": "$schedRoleArn",
    "Input": "$inputEscaped"
  }
}
"@
$scheduleFile = Join-Path $env:TEMP "arrol-fleet-schedule.json"
Write-JsonFile $scheduleFile $scheduleJson

$scheduleExists = $false
try {
  aws scheduler get-schedule --name $ScheduleName --region $Region --output json 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { $scheduleExists = $true }
} catch {}
if ($scheduleExists) {
  aws scheduler update-schedule --cli-input-json "file://$scheduleFile" --region $Region | Out-Null
  Write-Host "   updated existing schedule."
} else {
  aws scheduler create-schedule --cli-input-json "file://$scheduleFile" --region $Region | Out-Null
  Write-Host "   created schedule."
}
Remove-Item $scheduleFile -Force

# ---- Done --------------------------------------------------------------------
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT STEPS:" -ForegroundColor Yellow
Write-Host "  1. Build and push the image (from the worker repo root):"
Write-Host "     aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Account.dkr.ecr.$Region.amazonaws.com"
Write-Host "     docker build -f Dockerfile.fleet -t $EcrRepo ."
Write-Host "     docker tag ${EcrRepo}:latest $image"
Write-Host "     docker push $image"
Write-Host ""
Write-Host "  2. Submit a test job:"
Write-Host "     aws batch submit-job --job-name fleet-test --job-queue $QueueName --job-definition $JobDefName --region $Region"
Write-Host ""
Write-Host "  3. Watch it:"
Write-Host "     aws batch list-jobs --job-queue $QueueName --filters name=JOB_NAME,values=fleet-test --region $Region"
Write-Host "     (logs land in CloudWatch under /aws/batch/job)"
Write-Host ""
Write-Host "  The 15-minute schedule is live from now - it will submit jobs even before"
Write-Host "  the image is pushed; those simply fail to start and cost nothing. Push the"
Write-Host "  image and the next tick runs clean."