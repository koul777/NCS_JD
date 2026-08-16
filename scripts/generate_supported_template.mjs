#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import * as kordoc from "kordoc";


const fields = [
  "채용분야",
  "대분류",
  "중분류",
  "소분류",
  "세분류",
  "능력단위",
  "직무수행내용",
  "필요지식",
  "필요기술",
  "직무수행태도",
  "필요자격",
  "직업기초능력",
  "비고/근거",
];

const destination = path.resolve(
  process.argv[2] || "reports/samples/ncs-jd-supported-template.hwpx",
);
const markdown = [
  "# NCS 직무기술서 예시 양식",
  "",
  "> 아래 표의 왼쪽 필드명은 변경하지 마세요. 오른쪽 빈 칸에 생성 결과가 입력됩니다.",
  "",
  "| 항목 | 내용 |",
  "| --- | --- |",
  ...fields.map((field) => `| ${field} |  |`),
].join("\n");

const output = Buffer.from(await kordoc.markdownToHwpx(markdown, {
  gongmun: { preset: "보고서" },
}));
const validation = await kordoc.validateHwpx(output);
if (validation?.ok !== true) {
  throw new Error("Kordoc generated an invalid supported template");
}
const parsed = await kordoc.parseHwpx(output, { keepTrailingEmptyCols: true });
const schema = kordoc.extractFormSchema(parsed.blocks);
const labels = new Set((schema?.fields || []).map((field) => String(field.label || "").trim()));
const missing = fields.filter((field) => !labels.has(field));
if (missing.length > 0) {
  throw new Error(`Generated template is missing fields: ${missing.join(", ")}`);
}

await fs.mkdir(path.dirname(destination), { recursive: true });
await fs.writeFile(destination, output);
process.stdout.write(JSON.stringify({
  destination,
  bytes: output.length,
  fields,
  entry_count: validation.entryCount,
}, null, 2));
