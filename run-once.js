// run-once.js - single-job entry for per-job Fargate tasks (ECS RunTask).
//
// The legacy model ran index.js as an always-on HTTP service that received job
// triggers and queued them onto ONE shared container - so you paid for it 24/7
// and heavy stages fought over the same RAM. This entry runs exactly ONE job and
// exits, so every job gets its own right-sized, short-lived task: no idle billing,
// no shared-RAM OOM, and the in-memory queue is no longer needed.
//
// The portal launches it via the AWS SDK, roughly:
//
//   await ecs.runTask({
//     cluster: 'default',
//     launchType: 'FARGATE',
//     taskDefinition: 'default-arrol-worker-aaae',          // a big revision for per-job work
//     overrides: { containerOverrides: [{
//       name: '<container-name-from-task-def>',             // must match the task def
//       command: ['node', 'run-once.js'],                   // override the server CMD
//       environment: [
//         { name: 'MODE',    value: 'analyse' },             // 'ingest' | 'process' | 'analyse'
//         { name: 'PAYLOAD', value: JSON.stringify({...}) }, // the same body the HTTP route took
//       ],
//     }]},
//     networkConfiguration: { awsvpcConfiguration: {
//       subnets: ['subnet-00b7a29abc1488bed', 'subnet-0f555da69d9f84c7e'],
//       securityGroups: ['sg-08ff90433f71e4df7'],
//       assignPublicIp: 'ENABLED',
//     }},
//   })
//
// MODE / PAYLOAD contract (PAYLOAD is a JSON string):
//   ingest  -> { key, outKey, jobId }        COPC build      -> S3 + lidar_jobs
//   process -> { jobId, rawPath }            octree (legacy) -> S3 + lidar_jobs.octree_path
//   analyse -> { cloudJobId, analyses[] }    analysis chain  -> S3 + lidar_analyses
//
// Status/results still flow back through Supabase + S3 exactly as before; nothing
// about how the portal reads results changes. Requiring index.js runs its top-level
// setup (env, Supabase client) but NOT app.listen (guarded by require.main).

const { supabase, ingestCopc, convertJob, runAnalyses } = require('./index.js')

function clean(v) { return v ? v.trim().replace(/^['"]|['"]$/g, '').trim() : '' }

async function main() {
  const MODE = clean(process.env.MODE)
  let p
  try { p = JSON.parse(process.env.PAYLOAD || '{}') }
  catch (e) { console.error('[run-once] PAYLOAD is not valid JSON:', process.env.PAYLOAD); process.exit(2) }

  console.log(`[run-once] MODE=${MODE} PAYLOAD=${JSON.stringify(p)}`)

  if (MODE === 'ingest') {
    if (!p.key) { console.error('[run-once] ingest needs { key }'); process.exit(2) }
    await ingestCopc(p.key, p.outKey, p.jobId)
  } else if (MODE === 'process') {
    if (!p.jobId || !p.rawPath) { console.error('[run-once] process needs { jobId, rawPath }'); process.exit(2) }
    await convertJob(p.jobId, p.rawPath)
  } else if (MODE === 'analyse') {
    if (!p.cloudJobId || !Array.isArray(p.analyses) || !p.analyses.length) {
      console.error('[run-once] analyse needs { cloudJobId, analyses[] }'); process.exit(2)
    }
    await runAnalyses(p.cloudJobId, p.analyses)
  } else if (MODE === 'harvest') {
    if (!p.quoteId) { console.error('[run-once] harvest needs { quoteId }'); process.exit(2) }
    // Self-contained handler; writes its own queued->processing->ready|failed status.
    const { runHarvestQuote } = require('./harvest_quote.js')
    await runHarvestQuote(p)
  } else {
    console.error(`[run-once] unknown MODE '${MODE}' (expected ingest | process | analyse | harvest)`); process.exit(2)
  }
}

main()
  .then(() => { console.log('[run-once] done'); process.exit(0) })
  .catch(async (e) => {
    console.error('[run-once] FAILED:', e && e.stack ? e.stack : e)
    // ingestCopc throws on failure and the HTTP route used to write the failed status,
    // so replicate that one here. analyse writes per-analysis failure internally; octree
    // is an optional artifact whose failure must NOT flip any job status.
    try {
      const MODE = clean(process.env.MODE)
      const p = JSON.parse(process.env.PAYLOAD || '{}')
      if (MODE === 'ingest' && p.jobId && supabase) {
        await supabase.from('lidar_jobs')
          .update({ status: 'failed', error: String(e).slice(0, 500), updated_at: new Date().toISOString() })
          .eq('id', p.jobId)
      }
    } catch (_) {}
    process.exit(1)
  })
