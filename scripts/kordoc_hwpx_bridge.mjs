#!/usr/bin/env node

// Single-request HWPX bridge. Stdout is reserved for one JSON envelope and
// never receives markdown, source JSON, template bytes, or Kordoc internals.

const KORDOC_VERSION = "4.2.9"
const HARD_STDIN_LIMIT = 128 * 1024 * 1024
const MAX_MARKDOWN_CHARS = 8 * 1024 * 1024
const MAX_HWPX_BYTES = 64 * 1024 * 1024
const MAX_SVG_CHARS = 32 * 1024 * 1024
const MAX_TEMPLATE_FIELDS = 100
const MAX_FIELD_VALUE_CHARS = 1_000_000

console.log = () => {}
console.info = () => {}
console.debug = () => {}
console.warn = () => {}
console.error = () => {}

function reply(value) {
  process.stdout.write(JSON.stringify(value))
}

function safeString(value, maxLength = 500) {
  if (!["string", "number", "boolean"].includes(typeof value)) return ""
  return String(value).replaceAll("\u0000", "").trim().slice(0, maxLength)
}

function errorCode(value, fallback = "kordoc_error") {
  const code = safeString(value, 100).toLowerCase().replace(/[^a-z0-9_-]/g, "_")
  return code || fallback
}

function bridgeError(message, code) {
  return Object.assign(new Error(message), { code })
}

async function readStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > HARD_STDIN_LIMIT) throw bridgeError("bridge request is too large", "REQUEST_TOO_LARGE")
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString("utf8")
}

function positiveLimit(value, maximum, label) {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > maximum) {
    throw bridgeError(`${label} is invalid`, "INVALID_REQUEST")
  }
  return parsed
}

function decodeBase64(value, maxBytes, label = "content_base64") {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw bridgeError(`${label} is invalid`, "INVALID_REQUEST")
  }
  const bytes = Buffer.from(value, "base64")
  if (bytes.length > maxBytes) throw bridgeError(`${label} exceeds the configured size limit`, "DOCUMENT_TOO_LARGE")
  return bytes
}

function validationSummary(validation) {
  const issues = Array.isArray(validation?.issues) ? validation.issues : []
  const entryCount = Number(validation?.entryCount)
  return {
    ok: validation?.ok === true,
    entry_count: Number.isSafeInteger(entryCount) && entryCount >= 0 ? entryCount : 0,
    issue_count: issues.length,
  }
}

function emptyCapability(requested = false) {
  return {
    requested,
    supported: false,
    used: false,
    mode: "standard_generate",
    fallback_reason: requested ? "template_not_evaluated" : null,
    matched_fields: [],
    unmatched_fields: [],
    ambiguous_fields: [],
  }
}

function normalizeFieldValues(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw bridgeError("template field_values must be an object", "INVALID_REQUEST")
  }
  const entries = Object.entries(raw)
  if (entries.length === 0 || entries.length > MAX_TEMPLATE_FIELDS) {
    throw bridgeError("template field_values count is invalid", "INVALID_REQUEST")
  }
  const values = {}
  for (const [rawKey, rawValue] of entries) {
    const key = safeString(rawKey, 100)
    if (!key || typeof rawValue !== "string" || rawValue.length > MAX_FIELD_VALUE_CHARS) {
      throw bridgeError("template field_values contains an invalid entry", "INVALID_REQUEST")
    }
    if (Object.hasOwn(values, key)) {
      throw bridgeError("template field labels must be unique", "INVALID_REQUEST")
    }
    values[key] = rawValue.replaceAll("\u0000", "")
  }
  return values
}

function normalizeLabel(value) {
  return safeString(value, 100).replace(/[\s\p{P}\p{S}]+/gu, "").toLowerCase()
}

const FIELD_ALIASES = new Map([
  ["채용분야", "채용분야"], ["모집분야", "채용분야"], ["지원분야", "채용분야"],
  ["직무명", "채용분야"], ["직무", "채용분야"],
  ["대분류", "대분류"], ["중분류", "중분류"], ["소분류", "소분류"], ["세분류", "세분류"],
  ["능력단위", "능력단위"], ["역량단위", "능력단위"],
  ["직무수행내용", "직무수행내용"], ["담당업무", "직무수행내용"],
  ["수행업무", "직무수행내용"], ["주요업무", "직무수행내용"],
  ["주요책무", "직무수행내용"], ["책무", "직무수행내용"], ["과업", "직무수행내용"],
  ["필요지식", "필요지식"], ["지식", "필요지식"], ["지식요건", "필요지식"],
  ["필요기술", "필요기술"], ["기술", "필요기술"], ["기술요건", "필요기술"],
  ["직무수행태도", "직무수행태도"], ["필요태도", "직무수행태도"], ["태도", "직무수행태도"],
  ["필요자격", "필요자격"], ["자격", "필요자격"], ["자격요건", "필요자격"],
  ["응시자격", "필요자격"], ["응시자격요건", "필요자격"],
  ["직업기초능력", "직업기초능력"], ["기초능력", "직업기초능력"],
  ["비고근거", "비고근거"], ["비고", "비고근거"], ["근거", "비고근거"], ["출처", "비고근거"],
])

function canonicalLabel(value) {
  const normalized = normalizeLabel(value)
  return FIELD_ALIASES.get(normalized) ?? normalized
}

function mapValuesToSchema(schemaFields, sourceValues) {
  const targetsByCanonical = new Map()
  for (const field of schemaFields) {
    const label = safeString(field?.label, 100)
    if (!label) continue
    const canonical = canonicalLabel(label)
    const targets = targetsByCanonical.get(canonical) ?? []
    targets.push(label)
    targetsByCanonical.set(canonical, targets)
  }

  const values = {}
  const matched = []
  const unmatched = []
  const ambiguous = []
  for (const [sourceLabel, value] of Object.entries(sourceValues)) {
    const targets = [...new Set(targetsByCanonical.get(canonicalLabel(sourceLabel)) ?? [])]
    if (targets.length === 1) {
      values[targets[0]] = value
      matched.push(targets[0])
    } else if (targets.length > 1) {
      ambiguous.push(sourceLabel)
    } else {
      unmatched.push(sourceLabel)
    }
  }
  return { values, matched, unmatched, ambiguous }
}

function rowCells(block) {
  const table = block?.table && typeof block.table === "object" ? block.table : block
  return Array.isArray(table?.cells) ? table.cells : []
}

function sanitizeRebuiltBlocks(blocks, mappedValues) {
  const mappedCanonicals = new Set(Object.keys(mappedValues).map(canonicalLabel))
  const structuralLabels = new Set([...FIELD_ALIASES.keys(), "분류체계"])
  const jobTitleEntry = Object.entries(mappedValues).find(([label]) => canonicalLabel(label) === "채용분야")
  const jobTitle = safeString(jobTitleEntry?.[1], 300)

  for (const block of blocks) {
    if (block?.type === "heading" && jobTitle && /NCS\s*기반\s*채용\s*직무\s*설명자료/u.test(safeString(block?.text, 500))) {
      block.text = `[NCS 기반 채용 직무 설명자료 : ${jobTitle}]`
    }
    if (block?.type === "paragraph" && /본\s*직무수행\s*내용/u.test(safeString(block?.text, 500))) {
      block.text = "※ 본 문서는 NCS 근거를 참고해 생성한 검토용 초안입니다. 조직의 최종 검토와 승인이 필요합니다."
    }
    for (const cells of rowCells(block)) {
      if (!Array.isArray(cells)) continue
      const boundaryIndexes = []
      for (let index = 0; index < cells.length; index += 1) {
        if (structuralLabels.has(normalizeLabel(cells[index]?.text))) boundaryIndexes.push(index)
      }
      for (let position = 0; position < boundaryIndexes.length; position += 1) {
        const labelIndex = boundaryIndexes[position]
        const canonical = canonicalLabel(cells[labelIndex]?.text)
        if (!mappedCanonicals.has(canonical)) continue
        const targetIndex = labelIndex + 1
        const boundary = boundaryIndexes[position + 1] ?? cells.length
        for (let index = targetIndex + 1; index < boundary; index += 1) {
          if (cells[index] && typeof cells[index] === "object") cells[index].text = ""
        }
      }
    }
  }
  return blocks
}

async function rebuildParsedTemplate(kordoc, parsed, mappedValues, maxOutputBytes, capability) {
  const fill = kordoc.fillFormFields(parsed.blocks, mappedValues)
  const fillUnmatched = Array.isArray(fill?.unmatched)
    ? fill.unmatched.map((item) => safeString(item, 100)).filter(Boolean)
    : Object.keys(mappedValues)
  if (fillUnmatched.length > 0) {
    capability.unmatched_fields = [...new Set([...capability.unmatched_fields, ...fillUnmatched])]
    capability.fallback_reason = "template_fill_unmatched"
    return null
  }
  const sanitizedBlocks = sanitizeRebuiltBlocks(fill.blocks, mappedValues)
  const markdown = kordoc.blocksToMarkdown(sanitizedBlocks)
  const output = Buffer.from(await kordoc.markdownToHwpx(markdown))
  if (output.length > maxOutputBytes) {
    capability.fallback_reason = "template_output_too_large"
    return null
  }
  const validation = await kordoc.validateHwpx(output)
  if (validation?.ok !== true) {
    capability.fallback_reason = "template_fill_invalid_hwpx"
    return null
  }
  capability.supported = true
  capability.used = true
  capability.mode = "template_rebuild"
  capability.fallback_reason = null
  return {
    content_base64: output.toString("base64"),
    validation: validationSummary(validation),
    template_capability: capability,
  }
}

async function standardHwpx(kordoc, markdown, maxOutputBytes, capability) {
  const output = Buffer.from(await kordoc.markdownToHwpx(markdown, {
    gongmun: { preset: "보고서" },
  }))
  if (output.length > maxOutputBytes) {
    throw bridgeError("generated HWPX exceeds the configured size limit", "DOCUMENT_TOO_LARGE")
  }
  const validation = await kordoc.validateHwpx(output)
  if (validation?.ok !== true) {
    throw bridgeError("Kordoc generated an invalid HWPX", "INVALID_HWPX")
  }
  return {
    content_base64: output.toString("base64"),
    validation: validationSummary(validation),
    template_capability: capability,
  }
}

async function generateFromTemplate(kordoc, template, markdown, maxOutputBytes) {
  const capability = emptyCapability(true)
  const format = safeString(template?.format, 20).toLowerCase()
  if (!["hwpx", "pdf", "hwp"].includes(format)) {
    capability.fallback_reason = "template_format_not_supported"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  const maxTemplateBytes = positiveLimit(template?.max_bytes, MAX_HWPX_BYTES, "template max_bytes")
  const bytes = decodeBase64(template?.content_base64, maxTemplateBytes, "template content_base64")
  const sourceValues = normalizeFieldValues(template?.field_values)

  if (format !== "hwpx") {
    const detected = safeString(kordoc.detectFormat(bytes), 20).toLowerCase()
    if ((format === "pdf" && detected !== "pdf") || (format === "hwp" && !["hwp", "hwp3"].includes(detected))) {
      capability.fallback_reason = "template_format_mismatch"
      return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
    }
    let parsed
    try {
      parsed = await kordoc.parse(bytes, { keepTrailingEmptyCols: true })
    } catch {
      capability.fallback_reason = "template_parse_failed"
      return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
    }
    if (!parsed || parsed.success !== true || !Array.isArray(parsed.blocks)) {
      capability.fallback_reason = "template_parse_failed"
      return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
    }
    const schema = kordoc.extractFormSchema(parsed.blocks)
    const schemaFields = Array.isArray(schema?.fields) ? schema.fields : []
    const mapping = mapValuesToSchema(schemaFields, sourceValues)
    capability.matched_fields = mapping.matched
    capability.unmatched_fields = mapping.unmatched
    capability.ambiguous_fields = mapping.ambiguous
    if (capability.ambiguous_fields.length > 0) {
      capability.fallback_reason = "template_fields_ambiguous"
      return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
    }
    const mappedCanonicals = new Set(Object.keys(mapping.values).map(canonicalLabel))
    if (mapping.matched.length < 2 || !mappedCanonicals.has("직무수행내용")) {
      capability.fallback_reason = "template_required_fields_unmatched"
      return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
    }
    try {
      const rebuilt = await rebuildParsedTemplate(kordoc, parsed, mapping.values, maxOutputBytes, capability)
      if (rebuilt) return rebuilt
    } catch {
      capability.fallback_reason = "template_fill_failed"
    }
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  const detected = await kordoc.detectZipFormat(bytes)
  if (detected !== "hwpx") {
    capability.fallback_reason = "template_not_hwpx"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  const originalValidation = await kordoc.validateHwpx(bytes)
  if (originalValidation?.ok !== true) {
    capability.fallback_reason = "template_invalid_hwpx"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  let parsed
  try {
    parsed = await kordoc.parseHwpx(bytes, { keepTrailingEmptyCols: true })
  } catch {
    capability.fallback_reason = "template_parse_failed"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  if (!parsed || parsed.success !== true || !Array.isArray(parsed.blocks)) {
    capability.fallback_reason = "template_parse_failed"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  const schema = kordoc.extractFormSchema(parsed.blocks)
  const schemaFields = Array.isArray(schema?.fields) ? schema.fields : []
  const mapping = mapValuesToSchema(schemaFields, sourceValues)
  capability.matched_fields = mapping.matched
  capability.unmatched_fields = mapping.unmatched
  capability.ambiguous_fields = mapping.ambiguous
  if (capability.ambiguous_fields.length > 0) {
    capability.fallback_reason = "template_fields_ambiguous"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  const mappedCanonicals = new Set(Object.keys(mapping.values).map(canonicalLabel))
  if (mapping.matched.length < 2 || !mappedCanonicals.has("직무수행내용")) {
    capability.fallback_reason = "template_required_fields_unmatched"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  const matchedLabels = new Set(mapping.matched)
  const matchedFieldsContainExampleValues = schemaFields.some((field) => (
    matchedLabels.has(safeString(field?.label, 100)) && field?.empty !== true
  ))
  if (matchedFieldsContainExampleValues) {
    try {
      const rebuilt = await rebuildParsedTemplate(kordoc, parsed, mapping.values, maxOutputBytes, capability)
      if (rebuilt) return rebuilt
    } catch {
      capability.fallback_reason = "template_fill_failed"
    }
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  capability.supported = true
  let filled
  try {
    filled = await kordoc.fillForm(bytes, mapping.values, "hwpx-preserve")
  } catch {
    capability.fallback_reason = "template_fill_failed"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  if (filled?.format !== "hwpx-preserve" || !filled?.output) {
    capability.fallback_reason = "template_fill_failed"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  const fillUnmatched = Array.isArray(filled?.fill?.unmatched)
    ? filled.fill.unmatched.map((item) => safeString(item, 100)).filter(Boolean)
    : Object.keys(mapping.values)
  if (fillUnmatched.length > 0) {
    capability.unmatched_fields = fillUnmatched
    capability.fallback_reason = "template_fill_unmatched"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }

  const output = Buffer.from(filled.output)
  if (output.length > maxOutputBytes) {
    capability.fallback_reason = "template_output_too_large"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  const validation = await kordoc.validateHwpx(output)
  if (validation?.ok !== true) {
    capability.fallback_reason = "template_fill_invalid_hwpx"
    return standardHwpx(kordoc, markdown, maxOutputBytes, capability)
  }
  capability.used = true
  capability.mode = "hwpx-preserve"
  capability.fallback_reason = null
  return {
    content_base64: output.toString("base64"),
    validation: validationSummary(validation),
    template_capability: capability,
  }
}

async function generate(request, kordoc) {
  if (typeof request?.markdown !== "string" || !request.markdown.trim() || request.markdown.length > MAX_MARKDOWN_CHARS) {
    throw bridgeError("markdown is empty or too large", "INVALID_REQUEST")
  }
  const maxOutputBytes = positiveLimit(request.max_output_bytes, MAX_HWPX_BYTES, "max_output_bytes")
  if (request.template === undefined || request.template === null) {
    return standardHwpx(kordoc, request.markdown, maxOutputBytes, emptyCapability(false))
  }
  if (typeof request.template !== "object" || Array.isArray(request.template)) {
    throw bridgeError("template must be an object", "INVALID_REQUEST")
  }
  return generateFromTemplate(kordoc, request.template, request.markdown, maxOutputBytes)
}

async function inspectTemplate(request, kordoc) {
  const template = request?.template
  if (!template || typeof template !== "object" || Array.isArray(template)) {
    throw bridgeError("template must be an object", "INVALID_REQUEST")
  }
  const format = safeString(template?.format, 20).toLowerCase()
  if (!["hwpx", "pdf", "hwp"].includes(format)) {
    throw bridgeError("template format is not supported", "TEMPLATE_FORMAT_NOT_SUPPORTED")
  }
  const maxTemplateBytes = positiveLimit(template?.max_bytes, MAX_HWPX_BYTES, "template max_bytes")
  const bytes = decodeBase64(template?.content_base64, maxTemplateBytes, "template content_base64")
  const detected = safeString(kordoc.detectFormat(bytes), 20).toLowerCase()
  const compatible = detected === format || (format === "hwp" && detected === "hwp3")
  if (!compatible) throw bridgeError("template format does not match its content", "TEMPLATE_FORMAT_MISMATCH")

  let parsed
  try {
    parsed = await kordoc.parse(bytes, { keepTrailingEmptyCols: true })
  } catch {
    throw bridgeError("template could not be parsed", "TEMPLATE_PARSE_FAILED")
  }
  if (!parsed || parsed.success !== true || !Array.isArray(parsed.blocks)) {
    throw bridgeError("template could not be parsed", "TEMPLATE_PARSE_FAILED")
  }
  const schema = kordoc.extractFormSchema(parsed.blocks)
  const fields = (Array.isArray(schema?.fields) ? schema.fields : []).slice(0, MAX_TEMPLATE_FIELDS).map((field) => ({
    label: safeString(field?.label, 100),
    value_preview: safeString(field?.value, 500),
    empty: field?.empty === true,
    row: Number.isSafeInteger(field?.row) && field.row >= 0 ? field.row : null,
    col: Number.isSafeInteger(field?.col) && field.col >= 0 ? field.col : null,
  })).filter((field) => field.label)
  const confidence = Number(schema?.confidence)
  return {
    format: detected || format,
    confidence: Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0,
    fields,
  }
}

async function render(request, kordoc) {
  const maxInputBytes = positiveLimit(request?.max_input_bytes, MAX_HWPX_BYTES, "max_input_bytes")
  const maxSvgChars = positiveLimit(request?.max_svg_chars, MAX_SVG_CHARS, "max_svg_chars")
  const bytes = decodeBase64(request?.content_base64, maxInputBytes)
  const detected = await kordoc.detectZipFormat(bytes)
  if (detected !== "hwpx") throw bridgeError("render input must be HWPX", "INVALID_HWPX")
  const validation = await kordoc.validateHwpx(bytes)
  if (validation?.ok !== true) throw bridgeError("render input is not a valid HWPX", "INVALID_HWPX")
  const rendered = await kordoc.renderHwpxToSvg(bytes, { reflow: true })
  if (typeof rendered?.svg !== "string" || rendered.svg.length > maxSvgChars) {
    throw bridgeError("rendered SVG exceeds the configured size limit", "RENDER_OUTPUT_TOO_LARGE")
  }
  const pageCount = Number(rendered.pageCount)
  if (!Number.isSafeInteger(pageCount) || pageCount < 1) {
    throw bridgeError("Kordoc returned an invalid page count", "INVALID_RENDER_RESULT")
  }
  const warnings = Array.isArray(rendered.warnings)
    ? rendered.warnings.slice(0, 1_000).map((warning) => safeString(warning, 500)).filter(Boolean)
    : []
  return { svg: rendered.svg, page_count: pageCount, warnings }
}

try {
  const input = await readStdin()
  let request
  try {
    request = JSON.parse(input)
  } catch {
    throw bridgeError("stdin must contain one JSON object", "INVALID_JSON")
  }
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw bridgeError("stdin must contain one JSON object", "INVALID_REQUEST")
  }
  const kordoc = await import("kordoc")
  if (safeString(kordoc.VERSION, 30) !== KORDOC_VERSION) {
    throw bridgeError(`Kordoc ${KORDOC_VERSION} is required`, "KORDOC_VERSION_MISMATCH")
  }
  let result
  if (request.action === "generate") result = await generate(request, kordoc)
  else if (request.action === "inspect_template") result = await inspectTemplate(request, kordoc)
  else if (request.action === "render") result = await render(request, kordoc)
  else throw bridgeError("action must be generate, inspect_template, or render", "INVALID_REQUEST")
  reply({ ok: true, result })
} catch (error) {
  reply({
    ok: false,
    error: {
      code: errorCode(error?.code),
      message: safeString(error?.message, 500) || "Kordoc HWPX bridge failed",
    },
  })
  process.exitCode = 1
}
