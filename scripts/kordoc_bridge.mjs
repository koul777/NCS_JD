#!/usr/bin/env node

// Single-request JSON bridge. Stdout is reserved exclusively for one JSON
// response; source bytes and parser internals are never logged.

const HARD_STDIN_LIMIT = 128 * 1024 * 1024
const MAX_MARKDOWN_CHARS = 32 * 1024 * 1024
const MAX_BLOCK_TEXT_CHARS = 1_000_000
const MAX_CELL_TEXT_CHARS = 20_000

console.log = () => {}
console.info = () => {}
console.debug = () => {}

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

async function readStdin() {
  const chunks = []
  let size = 0
  for await (const chunk of process.stdin) {
    size += chunk.length
    if (size > HARD_STDIN_LIMIT) throw Object.assign(new Error("bridge request is too large"), { code: "REQUEST_TOO_LARGE" })
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString("utf8")
}

function decodeBase64(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(value) || value.length % 4 !== 0) {
    throw Object.assign(new Error("content_base64 is invalid"), { code: "INVALID_REQUEST" })
  }
  return Buffer.from(value, "base64")
}

function extractText(value, depth = 0, budget = { nodes: 0 }) {
  if (depth > 8 || budget.nodes++ > 10_000 || value == null) return ""
  if (typeof value === "string") return safeString(value, MAX_BLOCK_TEXT_CHARS)
  if (typeof value !== "object") return ""
  if (Array.isArray(value)) {
    return value.map((item) => extractText(item, depth + 1, budget)).filter(Boolean).join("\n")
  }
  const directKeys = ["text", "content", "value", "caption", "alt", "label", "title"]
  const direct = directKeys.map((key) => extractText(value[key], depth + 1, budget)).filter(Boolean)
  if (direct.length) return direct.join("\n")
  const nestedKeys = ["blocks", "items", "paragraphs", "children"]
  return nestedKeys.map((key) => extractText(value[key], depth + 1, budget)).filter(Boolean).join("\n")
}

function tableRows(block) {
  const table = block?.table && typeof block.table === "object" ? block.table : block
  const rows = Array.isArray(table?.rows) ? table.rows : []
  return rows.slice(0, 10_000).map((row) => {
    const cells = Array.isArray(row) ? row : Array.isArray(row?.cells) ? row.cells : []
    return cells.slice(0, 1_000).map((cell) => safeString(extractText(cell), MAX_CELL_TEXT_CHARS))
  })
}

function pageNumber(block) {
  const value = block?.pageNumber ?? block?.page_number ?? block?.page
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

function blockType(block) {
  const value = safeString(block?.type, 30).toLowerCase()
  return ["heading", "paragraph", "table", "list", "image", "separator"].includes(value) ? value : "unknown"
}

function normalizeBlock(block, blocksToMarkdown) {
  const type = blockType(block)
  const rows = type === "table" ? tableRows(block) : []
  let markdown = ""
  try {
    markdown = safeString(blocksToMarkdown([block]), MAX_BLOCK_TEXT_CHARS)
  } catch {
    markdown = safeString(extractText(block), MAX_BLOCK_TEXT_CHARS)
  }
  return {
    type,
    page_number: pageNumber(block),
    text: safeString(extractText(block), MAX_BLOCK_TEXT_CHARS),
    markdown,
    table_rows: rows,
  }
}

function scalar(metadata, ...keys) {
  for (const key of keys) {
    if (metadata && metadata[key] != null) return metadata[key]
  }
  return null
}

function positiveInteger(value) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

function normalizeMetadata(metadata) {
  const source = metadata && typeof metadata === "object" && !Array.isArray(metadata) ? metadata : {}
  return {
    title: safeString(scalar(source, "title"), 500) || null,
    author: safeString(scalar(source, "author", "creator"), 500) || null,
    subject: safeString(scalar(source, "subject", "description"), 1_000) || null,
    created_at: safeString(scalar(source, "createdAt", "created_at", "created"), 100) || null,
    modified_at: safeString(scalar(source, "modifiedAt", "modified_at", "modified"), 100) || null,
    page_count: positiveInteger(scalar(source, "pageCount", "page_count", "pages")),
  }
}

function normalizeWarnings(warnings) {
  if (!Array.isArray(warnings)) return []
  return warnings.slice(0, 1_000).map((warning) => {
    if (typeof warning === "string") return { code: "kordoc_warning", message: safeString(warning, 500) }
    return {
      code: errorCode(warning?.code, "kordoc_warning"),
      message: safeString(warning?.message ?? warning?.text, 500) || "Kordoc reported a parse warning",
      block_index: Number.isInteger(warning?.blockIndex) ? warning.blockIndex : null,
    }
  })
}

function decodeText(buffer) {
  try {
    return { text: new TextDecoder("utf-8", { fatal: true }).decode(buffer), warnings: [] }
  } catch {
    try {
      return {
        text: new TextDecoder("euc-kr", { fatal: true }).decode(buffer),
        warnings: [{ code: "text_encoding_fallback", message: "Text was decoded using EUC-KR/CP949." }],
      }
    } catch {
      throw Object.assign(new Error("text encoding is neither UTF-8 nor CP949"), { code: "UNSUPPORTED_TEXT_ENCODING" })
    }
  }
}

async function parseRequest(request) {
  if (!request || request.operation !== "parse") {
    throw Object.assign(new Error("operation must be parse"), { code: "INVALID_REQUEST" })
  }
  const expectedFormat = safeString(request.expected_format, 20).toLowerCase()
  const allowed = new Set(["pdf", "hwp", "hwpx", "docx", "txt"])
  if (!allowed.has(expectedFormat)) throw Object.assign(new Error("expected_format is unsupported"), { code: "INVALID_REQUEST" })
  const bytes = decodeBase64(request.content_base64)
  const maxBytes = Number(request.max_bytes)
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1 || bytes.length > maxBytes) {
    throw Object.assign(new Error("document exceeds the configured size limit"), { code: "DOCUMENT_TOO_LARGE" })
  }

  if (expectedFormat === "txt") {
    const decoded = decodeText(bytes)
    const text = safeString(decoded.text, MAX_MARKDOWN_CHARS)
    return {
      format: "txt",
      markdown: text,
      blocks: text ? [{ type: "paragraph", page_number: null, text, markdown: text, table_rows: [] }] : [],
      metadata: {},
      warnings: decoded.warnings,
    }
  }

  const kordoc = await import("kordoc")
  const detected = safeString(kordoc.detectFormat(bytes), 20).toLowerCase()
  const expectedDetection = expectedFormat
  const compatibleDetection = detected === expectedFormat || (expectedFormat === "hwp" && detected === "hwp3")
  if (!compatibleDetection) {
    throw Object.assign(new Error(`Kordoc detected ${detected || "unknown"}, expected ${expectedFormat}`), { code: "FORMAT_MISMATCH" })
  }
  // Text-layer PDF extraction uses pdfjs only. Bundling the optional local OCR
  // models adds hundreds of megabytes, so scanned/image-only documents are not
  // silently OCRed by this compact desktop build.
  const options = expectedFormat === "pdf" ? { ocr: false } : undefined
  const parsed = await kordoc.parse(bytes, options)
  if (!parsed || parsed.success !== true) {
    const failure = parsed?.error
    const code = typeof failure === "object" ? failure?.code : parsed?.code
    const message = typeof failure === "string" ? failure : failure?.message ?? parsed?.message
    throw Object.assign(new Error(safeString(message, 500) || "Kordoc could not parse the document"), {
      code: errorCode(code),
    })
  }
  const rawBlocks = Array.isArray(parsed.blocks) ? parsed.blocks : []
  return {
    format: detected || expectedDetection,
    markdown: safeString(parsed.markdown, MAX_MARKDOWN_CHARS),
    blocks: rawBlocks.slice(0, 100_000).map((block) => normalizeBlock(block, kordoc.blocksToMarkdown)),
    metadata: normalizeMetadata(parsed.metadata),
    warnings: normalizeWarnings(parsed.warnings),
  }
}

try {
  const input = await readStdin()
  let request
  try {
    request = JSON.parse(input)
  } catch {
    throw Object.assign(new Error("stdin must contain one JSON object"), { code: "INVALID_JSON" })
  }
  const result = await parseRequest(request)
  reply({ ok: true, result })
} catch (error) {
  reply({
    ok: false,
    error: {
      code: errorCode(error?.code),
      message: safeString(error?.message, 500) || "Kordoc bridge failed",
    },
  })
  process.exitCode = 1
}
