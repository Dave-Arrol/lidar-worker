# batch-setup.ps1 - create the AWS Batch compute environment, job queue and job
# definition for the Arrol LiDAR worker. Run from PowerShell with the arrol-lidar
# credentials AFTER attaching the arrol-batch-setup IAM policy.
#
# It reuses everything you already have: the same ECR image, execution role,
# subnets, security group, 16 vCPU / 120 GB sizing and 200 GB ephemeral storage.
# The worker's environment variables (incl. credentials) are copied straight from
# the existing task definition, so no secrets are typed or pasted anywhere.
#
# Safe to re-run: it skips the compute environment / queue if they already exist,
# and registering the job definition just adds a new revision.
#
# When it finishes it prints the two values to set in Vercel:
#   LIDAR_BATCH_QUEUE   and   LIDAR_BATCH_JOBDEF

$ErrorActionPreference = 'Stop'

# ---- config (from your task def + launch settings) ----
$Region   = 'eu-west-2'
$Image    = '994114819090.dkr.ecr.eu-west-2.amazonaws.com/arrol-worker:latest'
$ExecRole = 'arn:aws:iam::994114819090:role/service-role/ecsTaskExecutionRole'
$Subnets  = @('subnet-00b7a29abc1488bed', 'subnet-0f555da69d9f84c7e')
$SG       = 'sg-08ff90433f71e4df7'
$TaskDef  = 'default-arrol-worker-aaae'
$MaxVcpus = 16              # queue ceiling = known-safe quota; raise later for concurrency
$Vcpu     = '16'
$Memory   = '122880'       # 120 GB (MiB)
$Ephemeral = 200           # GiB
$CeName    = 'arrol-lidar-ce'
$QueueName = 'arrol-lidar-queue'
$JobDef    = 'arrol-lidar-worker'


# ---- 0) Batch service-linked role (idempotent) ----
Write-Host "0) Ensuring Batch service-linked role..."
try { aws iam create-service-linked-role --aws-service-name batch.amazonaws.com 2>$null | Out-Null } catch { }

# ---- 1) Compute environment (Fargate) ----
$ceExists = $false
try {
  $ce = aws batch describe-compute-environments --compute-environments $CeName --region $Region --output json | ConvertFrom-Json
  if ($ce.computeEnvironments.Count -gt 0) { $ceExists = $true }
} catch { }

if ($ceExists) {
  Write-Host "1) Compute environment '$CeName' already exists - skipping."
} else {
  Write-Host "1) Creating compute environment '$CeName' (maxvCpus=$MaxVcpus)..."
  $subnetsJson = '["' + ($Subnets -join '","') + '"]'
  $sgJson      = '["' + $SG + '"]'
  $ceJson = @"
{
  "computeEnvironmentName": "$CeName",
  "type": "MANAGED",
  "state": "ENABLED",
  "computeResources": {
    "type": "FARGATE",
    "maxvCpus": $MaxVcpus,
    "subnets": $subnetsJson,
    "securityGroupIds": $sgJson
  }
}
"@
  $ceFile = Join-Path $env:TEMP 'arrol-ce.json'
  [System.IO.File]::WriteAllText($ceFile, $ceJson)
  aws batch create-compute-environment --cli-input-json "file://$ceFile" --region $Region | Out-Null
  Remove-Item $ceFile -Force
}

# wait until VALID
Write-Host "   waiting for compute environment to become VALID..."
do {
  Start-Sleep -Seconds 5
  $desc = aws batch describe-compute-environments --compute-environments $CeName --region $Region --output json | ConvertFrom-Json
  $status = $desc.computeEnvironments[0].status
  $reason = $desc.computeEnvironments[0].statusReason
  Write-Host "     status: $status"
} while ($status -eq 'CREATING' -or $status -eq 'UPDATING')
if ($status -ne 'VALID') { throw "Compute environment is '$status' ($reason)" }

# ---- 2) Job queue ----
$qExists = $false
try {
  $q = aws batch describe-job-queues --job-queues $QueueName --region $Region --output json | ConvertFrom-Json
  if ($q.jobQueues.Count -gt 0) { $qExists = $true }
} catch { }

if ($qExists) {
  Write-Host "2) Job queue '$QueueName' already exists - skipping."
} else {
  Write-Host "2) Creating job queue '$QueueName'..."
  aws batch create-job-queue `
    --job-queue-name $QueueName --state ENABLED --priority 1 `
    --compute-environment-order "order=1,computeEnvironment=$CeName" `
    --region $Region | Out-Null
}

# ---- 3) Job definition (copies env + secrets from the existing task def) ----
Write-Host "3) Reading worker env from task def '$TaskDef'..."
$envJson = (aws ecs describe-task-definition --task-definition $TaskDef --region $Region --query "taskDefinition.containerDefinitions[0].environment" --output json | Out-String).Trim()
$secJson = (aws ecs describe-task-definition --task-definition $TaskDef --region $Region --query "taskDefinition.containerDefinitions[0].secrets" --output json | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($envJson) -or $envJson -eq 'null') { $envJson = '[]' }
if ([string]::IsNullOrWhiteSpace($secJson) -or $secJson -eq 'null') { $secJson = '[]' }

# Splice the raw JSON arrays straight in (avoids PowerShell array-serialisation quirks).
$jobDefJson = @"
{
  "jobDefinitionName": "$JobDef",
  "type": "container",
  "platformCapabilities": ["FARGATE"],
  "containerProperties": {
    "image": "$Image",
    "command": ["node", "run-once.js"],
    "executionRoleArn": "$ExecRole",
    "resourceRequirements": [
      { "type": "VCPU", "value": "$Vcpu" },
      { "type": "MEMORY", "value": "$Memory" }
    ],
    "environment": $envJson,
    "secrets": $secJson,
    "ephemeralStorage": { "sizeInGiB": $Ephemeral },
    "networkConfiguration": { "assignPublicIp": "ENABLED" },
    "fargatePlatformConfiguration": { "platformVersion": "LATEST" }
  },
  "retryStrategy": { "attempts": 1 },
  "timeout": { "attemptDurationSeconds": 7200 }
}
"@
$jdFile = Join-Path $env:TEMP 'arrol-jobdef.json'
[System.IO.File]::WriteAllText($jdFile, $jobDefJson)
Write-Host "   registering job definition '$JobDef'..."
aws batch register-job-definition --cli-input-json "file://$jdFile" --region $Region | Out-Null
Remove-Item $jdFile -Force

Write-Host ""
Write-Host "============================================================"
Write-Host " Batch is set up. Set these in Vercel (and your local .env):"
Write-Host "   LIDAR_BATCH_QUEUE  = $QueueName"
Write-Host "   LIDAR_BATCH_JOBDEF = $JobDef"
Write-Host "============================================================"