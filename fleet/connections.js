// connections.js — machine_connections access + credential resolution.
//
// Phase B makes the feed settings-driven: each vendor feed is a row in
// machine_connections (status, frequency, options, Secrets Manager reference).
// Credentials NEVER live in the database — secret_name points at a Secrets
// Manager secret, and secret_json_key optionally names a key inside its JSON
// body (the seeded Komatsu connection reuses the original arrol/fleet secret
// via its KOMATSU_API_KEY key; connections added from the portal get their own
// secret at arrol/fleet/conn/{id}).
//
// The Fargate task role supplies AWS credentials; it needs
// secretsmanager:GetSecretValue on arrol/fleet* (see the Phase B setup steps).

'use strict'

const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager')

const AWS_REGION = process.env.AWS_REGION || 'eu-west-2'
const sm = new SecretsManagerClient({ region: AWS_REGION })

/** Load one connection by id, or all with the given statuses (default active). */
async function loadConnections(supabase, opts) {
  const { id, statuses } = opts || {}
  let q = supabase.from('machine_connections').select('*')
  if (id) q = q.eq('id', id)
  else q = q.in('status', statuses || ['active'])
  const { data, error } = await q.order('created_at', { ascending: true })
  if (error) throw new Error('machine_connections load failed: ' + error.message)
  return data || []
}

/**
 * Resolve the credential string for a connection from Secrets Manager.
 * Returns null when the connection has no secret_name (e.g. Deere, whose app
 * credentials arrive via the job definition env and whose OAuth token lives
 * in deere_connections).
 */
async function resolveCredential(conn) {
  if (!conn.secret_name) return null
  const res = await sm.send(new GetSecretValueCommand({ SecretId: conn.secret_name }))
  const raw = res.SecretString || ''
  if (!raw) throw new Error(`secret ${conn.secret_name} has no string value`)
  if (conn.secret_json_key) {
    let obj
    try { obj = JSON.parse(raw) } catch {
      throw new Error(`secret ${conn.secret_name} is not JSON but secret_json_key is set`)
    }
    const v = obj[conn.secret_json_key]
    if (!v) throw new Error(`secret ${conn.secret_name} has no key ${conn.secret_json_key}`)
    return String(v)
  }
  // No key named: accept {"api_key": "..."} JSON or a raw string secret.
  try {
    const obj = JSON.parse(raw)
    if (obj && typeof obj === 'object') return String(obj.api_key || obj.API_KEY || raw)
  } catch { /* raw string secret */ }
  return raw
}

/** Record the outcome of a run on the connection row (dispatcher owns next_run_at). */
async function recordRunResult(supabase, connectionId, ok, errorMessage) {
  await supabase.from('machine_connections').update({
    last_run_at: new Date().toISOString(),
    last_run_ok: !!ok,
    last_error: ok ? '' : String(errorMessage || '').slice(0, 500),
    updated_at: new Date().toISOString(),
  }).eq('id', connectionId)
}

module.exports = { loadConnections, resolveCredential, recordRunResult }
