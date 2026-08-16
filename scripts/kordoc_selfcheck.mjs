// Fail the launcher before it starts serving if the installed Kordoc is not the
// exact build the bridges are written against.  Lives beside them as a file
// rather than inline in the launcher so node resolves node_modules from the
// project and Windows PowerShell cannot mangle it into an unparseable argument.
const kordoc = await import("kordoc")

const REQUIRED_VERSION = "4.2.9"
const REQUIRED_EXPORTS = [
  "blocksToMarkdown",
  "detectFormat",
  "detectZipFormat",
  "extractFormSchema",
  "fillForm",
  "markdownToHwpx",
  "parse",
  "parseHwpx",
  "renderHwpxToSvg",
  "validateHwpx",
]

if (kordoc.VERSION !== REQUIRED_VERSION) {
  console.error(`Kordoc ${REQUIRED_VERSION} is required, found ${kordoc.VERSION}`)
  process.exit(2)
}

const missing = REQUIRED_EXPORTS.filter((name) => typeof kordoc[name] !== "function")
if (missing.length > 0) {
  console.error(`Kordoc is missing required exports: ${missing.join(", ")}`)
  process.exit(3)
}
