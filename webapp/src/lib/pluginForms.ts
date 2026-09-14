/**
 * Form models + (de)serialization helpers for the plugin editor.
 *
 * Keeps `PluginEditorDialog.vue` thin: slug auto-generation, conversion
 * between `Plugin` API objects and editable form rows, payload
 * normalization for create/update, and client-side validation.
 */

import type {
  Plugin,
  PluginCreateIn,
  PluginCredentialRequirementIn,
  PluginCredentialServiceType,
  PluginMcpServerIn,
  PluginMcpTransport,
  PluginSkillIn,
  PluginUpdateIn,
} from '@/types'

/** Narrow transport union (mirrors `PluginMcpTransport` from `@/types`). */
export type PluginMcpTransportOption = PluginMcpTransport

/** Narrow credential-service union (mirrors `PluginCredentialServiceType`). */
export type PluginServiceTypeOption = PluginCredentialServiceType

/** Exact placeholder example shown in the editor help texts. */
export const PLACEHOLDER_EXAMPLE = '{{credential.KEY}}'

export interface KeyValueRow {
  uid: string
  key: string
  value: string
}

export interface PluginSkillForm {
  uid: string
  name: string
  slug: string
  body: string
}

export interface PluginMcpForm {
  uid: string
  name: string
  slug: string
  transport: PluginMcpTransportOption
  command: string
  argsText: string
  cwd: string
  env: KeyValueRow[]
  headers: KeyValueRow[]
  url: string
  startupTimeout: number
  requestTimeout: number
}

export interface PluginRequirementForm {
  uid: string
  reqKey: string
  description: string
  required: boolean
  mode: 'existing' | 'new'
  serviceId: string
  serviceName: string
  serviceSlug: string
  credentialType: PluginServiceTypeOption
  envVarName: string
  targetPath: string
  label: string
}

export interface PluginFormModel {
  name: string
  slug: string
  slugTouched: boolean
  description: string
  enabled: boolean
  published: boolean
  skills: PluginSkillForm[]
  mcps: PluginMcpForm[]
  requirements: PluginRequirementForm[]
}

let uidCounter = 0

/** Unique row key for v-for lists (never sent to the backend). */
export function newUid(prefix = 'row'): string {
  uidCounter += 1
  return `${prefix}-${Date.now().toString(36)}-${uidCounter}`
}

/** URL-safe slug derivation (mirrors backend `slugify` behavior loosely). */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
}

export function emptyKeyValueRow(): KeyValueRow {
  return { uid: newUid('kv'), key: '', value: '' }
}

export function emptySkillForm(): PluginSkillForm {
  return { uid: newUid('skill'), name: '', slug: '', body: '' }
}

export function emptyMcpForm(): PluginMcpForm {
  return {
    uid: newUid('mcp'),
    name: '',
    slug: '',
    transport: 'stdio',
    command: '',
    argsText: '',
    cwd: '/workspace',
    env: [],
    headers: [],
    url: '',
    startupTimeout: 30,
    requestTimeout: 60,
  }
}

export function emptyRequirementForm(): PluginRequirementForm {
  return {
    uid: newUid('req'),
    reqKey: '',
    description: '',
    required: true,
    mode: 'existing',
    serviceId: '',
    serviceName: '',
    serviceSlug: '',
    credentialType: 'env',
    envVarName: '',
    targetPath: '',
    label: '',
  }
}

export function emptyPluginForm(): PluginFormModel {
  return {
    name: '',
    slug: '',
    slugTouched: false,
    description: '',
    enabled: true,
    published: true,
    skills: [],
    mcps: [],
    requirements: [],
  }
}

function toTransport(value: string): PluginMcpTransportOption {
  if (value === 'streamable_http' || value === 'sse') return value
  return 'stdio'
}

function toServiceType(value: string): PluginServiceTypeOption {
  if (value === 'file' || value === 'ssh_key') return value
  return 'env'
}

function dictToRows(dict: Record<string, string> | undefined, prefix: string): KeyValueRow[] {
  return Object.entries(dict ?? {}).map(([key, value]) => ({
    uid: newUid(prefix),
    key,
    value,
  }))
}

/**
 * Build an editable form from an existing plugin.
 *
 * Credential requirements always round-trip via `service_id` (mode
 * `existing`) so PATCH never creates duplicate credential services.
 */
export function pluginToForm(plugin: Plugin): PluginFormModel {
  return {
    name: plugin.name,
    slug: plugin.slug,
    slugTouched: true,
    description: plugin.description ?? '',
    enabled: plugin.enabled,
    published: plugin.published,
    skills: [...(plugin.skills ?? [])]
      .sort((a, b) => a.position - b.position)
      .map((s) => ({ uid: newUid('skill'), name: s.name, slug: s.slug, body: s.body })),
    mcps: (plugin.mcp_servers ?? []).map((m) => ({
      uid: newUid('mcp'),
      name: m.name,
      slug: m.slug,
      transport: toTransport(m.transport),
      command: m.command ?? '',
      argsText: (m.args ?? []).join('\n'),
      cwd: m.cwd || '/workspace',
      env: dictToRows(m.env, 'env'),
      headers: dictToRows(m.headers, 'hdr'),
      url: m.url ?? '',
      startupTimeout: m.startup_timeout_seconds ?? 30,
      requestTimeout: m.request_timeout_seconds ?? 60,
    })),
    requirements: (plugin.credential_requirements ?? []).map((r) => ({
      uid: newUid('req'),
      reqKey: r.key,
      description: r.description ?? '',
      required: r.required,
      mode: 'existing' as const,
      serviceId: r.service_id,
      serviceName: r.service_name,
      serviceSlug: r.service_slug,
      credentialType: toServiceType(r.credential_type),
      envVarName: '',
      targetPath: '',
      label: '',
    })),
  }
}

/** One argument per line → string list (trimmed, empties dropped). */
export function parseArgsText(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
}

/** Key/value rows → mapping (rows with blank keys are dropped). */
export function rowsToDict(rows: KeyValueRow[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    out[key] = row.value
  }
  return out
}

function skillToIn(skill: PluginSkillForm, index: number): PluginSkillIn {
  return {
    name: skill.name.trim(),
    slug: skill.slug.trim(),
    body: skill.body,
    position: index,
  }
}

function mcpToIn(mcp: PluginMcpForm): PluginMcpServerIn {
  const startup = Number(mcp.startupTimeout)
  const request = Number(mcp.requestTimeout)
  const base = {
    name: mcp.name.trim(),
    slug: mcp.slug.trim(),
    transport: mcp.transport,
    cwd: mcp.cwd.trim() || '/workspace',
    startup_timeout_seconds: Number.isFinite(startup) ? Math.trunc(startup) : 30,
    request_timeout_seconds: Number.isFinite(request) ? Math.trunc(request) : 60,
  }
  if (mcp.transport === 'stdio') {
    // Backend forbids URL for stdio; headers are validated but never
    // injected — clear both so stale values cannot hide in the payload.
    return { ...base, command: mcp.command.trim(), args: parseArgsText(mcp.argsText), env: rowsToDict(mcp.env), url: '', headers: {} }
  }
  // Backend forbids command/args for http/sse; env is validated but never
  // injected — clear both so stale values cannot hide in the payload.
  return { ...base, command: '', args: [], env: {}, url: mcp.url.trim(), headers: rowsToDict(mcp.headers) }
}

function requirementToIn(req: PluginRequirementForm): PluginCredentialRequirementIn {
  if (req.mode === 'existing') {
    return {
      key: req.reqKey.trim(),
      description: req.description.trim(),
      required: req.required,
      credential_service: { service_id: req.serviceId },
    }
  }
  return {
    key: req.reqKey.trim(),
    description: req.description.trim(),
    required: req.required,
    credential_service: {
      name: req.serviceName.trim(),
      slug: req.serviceSlug.trim(),
      description: '',
      credential_type: req.credentialType,
      env_var_name: req.envVarName.trim().toUpperCase(),
      target_path: req.targetPath.trim(),
      label: req.label.trim(),
    },
  }
}

/** Normalize the form into a create payload (backend fills empty slugs). */
export function formToCreateIn(form: PluginFormModel): PluginCreateIn {
  return {
    name: form.name.trim(),
    slug: form.slug.trim(),
    description: form.description.trim(),
    enabled: form.enabled,
    published: form.published,
    skills: form.skills.map((s, i) => skillToIn(s, i)),
    mcp_servers: form.mcps.map(mcpToIn),
    credential_requirements: form.requirements.map(requirementToIn),
  }
}

/** Normalize the form into an update payload (lists replace fully). */
export function formToUpdateIn(form: PluginFormModel): PluginUpdateIn {
  const create = formToCreateIn(form)
  return {
    name: create.name,
    slug: create.slug,
    description: create.description,
    enabled: create.enabled,
    published: create.published,
    skills: create.skills,
    mcp_servers: create.mcp_servers,
    credential_requirements: create.credential_requirements,
  }
}

const ENV_VAR_RE = /^[A-Z_][A-Z0-9_]*$/
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const KEY_RE = /^[a-z0-9_]+(?:[a-z0-9_-]*[a-z0-9_])?$/i
/** A stdio command must be a single executable (no whitespace/shell metacharacters). */
function hasCommandMetacharacters(command: string): boolean {
  if (/\s/.test(command)) return true
  if (command.includes('\0')) return true
  return /[;|&$`]/.test(command)
}
const HEADER_NAME_RE = /^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$/
const SUPPORTED_PLACEHOLDER_RE = /\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}/g
const GENERIC_PLACEHOLDER_RE = /\{\{|\}\}/

/** Client-side validation; mirrors the backend rules (see backend/apps/plugins/services.py). */
export function validatePluginForm(form: PluginFormModel): string[] {
  const errors: string[] = []
  if (!form.name.trim()) {
    errors.push('Plugin name is required.')
  } else if (form.name.trim().length > 255) {
    errors.push('Plugin name must be at most 255 characters.')
  }
  const slug = form.slug.trim() || slugify(form.name)
  if (form.name.trim() && (!slug || slug.length > 255 || !SLUG_RE.test(slug))) {
    errors.push('Plugin slug must be URL-safe (lowercase, digits, dashes).')
  }
  if (form.description.trim().length > 10000) {
    errors.push('Plugin description is too long (max 10000 characters).')
  }

  form.skills.forEach((skill, i) => {
    const label = `Skill #${i + 1}`
    if (!skill.name.trim()) errors.push(`${label}: name is required.`)
    else if (skill.name.trim().length > 255) errors.push(`${label}: name is too long.`)
    const skillSlug = skill.slug.trim() || slugify(skill.name)
    if (skill.name.trim() && (!skillSlug || !SLUG_RE.test(skillSlug))) {
      errors.push(`${label}: slug must be URL-safe (lowercase, digits, dashes).`)
    }
    if (!skill.body.trim()) errors.push(`${label}: body (Markdown) is required.`)
    else if (skill.body.length > 200000) errors.push(`${label}: body is too long.`)
  })

  const requirementKeys = new Set(form.requirements.map((r) => r.reqKey.trim()).filter(Boolean))

  form.mcps.forEach((mcp, i) => {
    const label = mcp.name.trim() ? `MCP server "${mcp.name.trim()}"` : `MCP server #${i + 1}`
    if (!mcp.name.trim()) errors.push(`${label}: name is required.`)
    else if (mcp.name.trim().length > 255) errors.push(`${label}: name is too long.`)
    const mcpSlug = mcp.slug.trim() || slugify(mcp.name)
    if (mcp.name.trim() && (!mcpSlug || !SLUG_RE.test(mcpSlug))) {
      errors.push(`${label}: slug must be URL-safe (lowercase, digits, dashes).`)
    }
    if (mcp.transport === 'stdio') {
      const command = mcp.command.trim()
      if (!command) {
        errors.push(`${label}: command is required for stdio transport.`)
      } else if (command.length > 1024 || hasCommandMetacharacters(command)) {
        errors.push(`${label}: command must be a single executable (no spaces or shell metacharacters like ; | & $ \` ).`)
      }
      if (parseArgsText(mcp.argsText).some((a) => a.length > 1024 || a.includes('\x00'))) {
        errors.push(`${label}: args must not contain null bytes and must be at most 1024 characters each.`)
      }
      if (mcp.url.trim()) errors.push(`${label}: URL must be empty for stdio transport.`)
    } else {
      const url = mcp.url.trim()
      if (!/^https?:\/\//i.test(url)) {
        errors.push(`${label}: URL must be an http(s) URL for http/sse transports.`)
      } else if (!isSafeHttpUrl(url)) {
        errors.push(`${label}: URL must not contain userinfo or a fragment.`)
      } else if (url.length > 2048) {
        errors.push(`${label}: URL is too long (max 2048 characters).`)
      }
      if (mcp.command.trim() || parseArgsText(mcp.argsText).length > 0) {
        errors.push(`${label}: command/args must be empty for http/sse transports.`)
      }
    }
    const activeRows = mcp.transport === 'stdio' ? mcp.env : mcp.headers
    const mappingErrors = validateMappingRows(activeRows, label, mcp.transport === 'stdio' ? 'env' : 'headers', requirementKeys)
    errors.push(...mappingErrors)
    // The unused mapping still round-trips through backend validation,
    // so surface its errors too (backend never injects it at runtime).
    const inactiveRows = mcp.transport === 'stdio' ? mcp.headers : mcp.env
    const inactiveErrors = validateMappingRows(inactiveRows, label, mcp.transport === 'stdio' ? 'headers' : 'env', requirementKeys)
    errors.push(...inactiveErrors)
    const startup = Number(mcp.startupTimeout)
    const request = Number(mcp.requestTimeout)
    if (!Number.isFinite(startup) || startup < 1 || startup > 600) {
      errors.push(`${label}: startup timeout must be between 1 and 600 seconds.`)
    }
    if (!Number.isFinite(request) || request < 1 || request > 600) {
      errors.push(`${label}: request timeout must be between 1 and 600 seconds.`)
    }
  })

  const seenKeys = new Set<string>()
  const seenServices = new Set<string>()
  form.requirements.forEach((req, i) => {
    const key = req.reqKey.trim()
    const label = key ? `Requirement "${key}"` : `Requirement #${i + 1}`
    if (!key) {
      errors.push(`${label}: key is required.`)
    } else if (key.length > 255 || !KEY_RE.test(key)) {
      errors.push(`${label}: key must be a stable identifier (letters, digits, dashes, underscores).`)
    } else if (seenKeys.has(key)) {
      errors.push(`${label}: duplicate key.`)
    } else {
      seenKeys.add(key)
    }
    if (req.description.trim().length > 2000) {
      errors.push(`${label}: description is too long (max 2000 characters).`)
    }
    if (req.mode === 'existing') {
      if (!req.serviceId) {
        errors.push(`${label}: select an existing credential service.`)
      } else if (seenServices.has(req.serviceId)) {
        errors.push(`${label}: duplicate credential service in requirements.`)
      } else {
        seenServices.add(req.serviceId)
      }
    } else {
      if (!req.serviceName.trim()) errors.push(`${label}: new service name is required.`)
      if (req.credentialType === 'env' && !ENV_VAR_RE.test(req.envVarName.trim().toUpperCase())) {
        errors.push(`${label}: environment variable name must look like OPENAI_API_KEY.`)
      }
      if (req.credentialType === 'file' && !req.targetPath.trim()) {
        errors.push(`${label}: target path is required for file services.`)
      }
    }
  })

  return errors
}

/** Reject userinfo (`user@host`) and `#fragment` URLs client-side. */
function isSafeHttpUrl(url: string): boolean {
  try {
    const parsed = new URL(url)
    if (parsed.username || parsed.password) return false
    if (parsed.hash) return false
    return true
  } catch {
    return false
  }
}

function validateMappingRows(
  rows: KeyValueRow[],
  label: string,
  field: string,
  requirementKeys: Set<string>,
): string[] {
  const errors: string[] = []
  const seen = new Set<string>()
  const isEnv = field === 'env'
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    if (key.length > 256) {
      errors.push(`${label}: ${field} key "${key}" is too long (max 256 characters).`)
      continue
    }
    if (isEnv && !ENV_VAR_RE.test(key.toUpperCase())) {
      // Backend accepts arbitrary mapping keys; env vars are conventionally
      // UPPER_SNAKE_CASE — warn-level strictness would be friendlier, but
      // keep it an error to match the placeholder-credential drift risk.
      errors.push(`${label}: env key "${key}" must look like OPENAI_API_KEY.`)
    }
    if (!isEnv && !HEADER_NAME_RE.test(key)) {
      errors.push(`${label}: header name "${key}" contains invalid characters.`)
    }
    if (seen.has(key)) {
      errors.push(`${label}: duplicate ${field} key "${key}".`)
    } else {
      seen.add(key)
    }
    if (row.value.length > 4096) {
      errors.push(`${label}: ${field} value for "${key}" is too long (max 4096 characters).`)
    }
    const referenced = referencedPlaceholderKeys(row.value)
    for (const ref of referenced) {
      if (!requirementKeys.has(ref)) {
        errors.push(`${label}: ${field} "${key}" references unknown credential "${ref}" (add a matching requirement key, e.g. ${PLACEHOLDER_EXAMPLE}).`)
      }
    }
    const stripped = row.value.replace(SUPPORTED_PLACEHOLDER_RE, '')
    SUPPORTED_PLACEHOLDER_RE.lastIndex = 0
    if (GENERIC_PLACEHOLDER_RE.test(stripped)) {
      errors.push(`${label}: ${field} "${key}" contains an unsupported placeholder (only ${PLACEHOLDER_EXAMPLE} is allowed).`)
    }
  }
  return errors
}

/** Extract `credential.KEY` references from a mapping value. */
export function referencedPlaceholderKeys(value: string): string[] {
  const keys: string[] = []
  SUPPORTED_PLACEHOLDER_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = SUPPORTED_PLACEHOLDER_RE.exec(value)) !== null) {
    keys.push(match[1]!)
  }
  SUPPORTED_PLACEHOLDER_RE.lastIndex = 0
  return keys
}
