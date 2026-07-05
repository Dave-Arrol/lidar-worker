// dispatch.js — the DB-driven scheduler tick.
//
// The single EventBridge schedule (15 min) submits the default Batch job with
// no CONNECTION_ID, which routes here (see sync.js main). This selects every
// active connection whose next_run_at has arrived, submits one per-connection
// Batch job (CONNECTION_ID env override), and bumps next_run_at by the row's
// frequency. Pausing a feed or changing its frequency is now just a row update
// from the portal — the AWS console is never needed again.
//
// FLEET_FORCE=1 treats every active connection as due (the portal's "Sync now"
// path); next_run_at still advances, which is correct — a forced sync IS a run.
//
// Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
//      FLEET_BATCH_QUEUE (default arrol-fleet-queue),
//      FLEET_BATCH_JOBDEF (default arrol-fleet-sync), AWS_REGION.

'use strict'

const { BatchClient, SubmitJobCommand } = require('@aws-sdk/client-batch')
const { loadConnections } = require('./connections')

const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'
const JOB_QUEUE = process.env.FLEET_BATCH_QUEUE || 'arrol-fleet-queue'
const JOB_DEF = process.env.FLEET_BATCH_JOBDEF || 'arrol-fleet-sync'

const batch = new BatchClient({ region: AWS_REGION })

async function runDispatch(supabase) {
  const force = process.env.FLEET_FORCE === '1'
  const now = new Date()
  const all = await loadConnections(supabase, { statuses: ['active'] })
  const due = force ? all : all.filter(c => new Date(c.next_run_at) <= now)
  console.log(`[dispatch] ${all.length} active connection(s), ${due.length} due${force ? ' (forced)' : ''}`)

  for (const conn of due) {
    const jobName = `arrol-fleet-${conn.vendor}-${Date.now()}`.replace(/[^A-Za-z0-9_-]/g, '-').slice(0, 128)
    try {
      const res = await batch.send(new SubmitJobCommand({
        jobName,
        jobQueue: JOB_QUEUE,
        jobDefinition: JOB_DEF,
        containerOverrides: {
          environment: [{ name: 'CONNECTION_ID', value: conn.id }],
        },
      }))
      const next = new Date(now.getTime() + conn.frequency_hours * 3600 * 1000)
      await supabase.from('machine_connections').update({
        next_run_at: next.toISOString(),
        updated_at: new Date().toISOString(),
      }).eq('id', conn.id)
      console.log(`[dispatch] ${conn.vendor} "${conn.label}" -> job ${res.jobId}, next run ${next.toISOString()}`)
    } catch (e) {
      console.error(`[dispatch] submit failed for ${conn.vendor} "${conn.label}": ${e.message}`)
    }
  }
  console.log('[dispatch] tick complete')
}

module.exports = { runDispatch }

// Standalone entry: node dispatch.js (manual tick).
if (require.main === module) {
  const { createClient } = require('@supabase/supabase-js')
  const url = process.env.SUPABASE_URL
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!url || !key) { console.error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not configured'); process.exit(1) }
  const supabase = createClient(url, key, { auth: { persistSession: false } })
  runDispatch(supabase).then(() => process.exit(0), e => { console.error('[dispatch] fatal:', e); process.exit(1) })
}
