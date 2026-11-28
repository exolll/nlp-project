import type { Config } from './api'

export interface FileMeta { name: string; size: number }

export function fileKind(name: string): 'PDF' | 'DOCX' | null {
  if (/\.pdf$/i.test(name)) return 'PDF'
  if (/\.docx$/i.test(name)) return 'DOCX'
  return null
}

export function fileLabel(file: FileMeta): string {
  return `${fileKind(file.name) ?? '文档'} · ${(file.size / 1024).toFixed(1)} KB`
}

export function inputLimit(config: Pick<Config, 'max_primary_characters' | 'max_supplement_characters'> | null, hasFile: boolean): number {
  if (!config) return 0
  return hasFile ? config.max_supplement_characters : config.max_primary_characters
}

export function validateFileChoice(files: FileMeta[], config: Pick<Config, 'max_file_bytes'> | null): string {
  if (files.length !== 1) return '每次仅支持一个文件，请选择单个 PDF 或 DOCX。'
  const [candidate] = files
  if (!fileKind(candidate.name)) return '仅支持 PDF 或 DOCX 文件。'
  if (!candidate.size) return '不能上传空文件。'
  if (!config) return '请等待服务配置加载后再选择文件。'
  if (candidate.size > config.max_file_bytes) return '文件超过服务允许的大小。'
  return ''
}