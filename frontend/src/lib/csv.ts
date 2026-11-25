export function parseCsv(text: string, limit = 200): { headers: string[]; rows: string[][] } {
  const clean = text.replace(/^﻿/, '')
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false

  for (let index = 0; index < clean.length; index += 1) {
    const char = clean[index]
    if (quoted) {
      if (char === '"') {
        if (clean[index + 1] === '"') {
          cell += '"'
          index += 1
        } else {
          quoted = false
        }
      } else {
        cell += char
      }
    } else if (char === '"') {
      quoted = true
    } else if (char === ',') {
      row.push(cell)
      cell = ''
    } else if (char === '\n') {
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
      if (rows.length >= limit + 1) break
    } else if (char !== '\r') {
      cell += char
    }
  }
  if (cell || row.length) {
    row.push(cell)
    rows.push(row)
  }
  const headers = rows[0] || []
  return { headers, rows: rows.slice(1, limit + 1) }
}
