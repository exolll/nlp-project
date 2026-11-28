// 生成 v4 UUID。
// `crypto.randomUUID` 只在安全上下文（HTTPS / localhost）可用；本项目部署在
// 纯 HTTP 的 IP 上时它是 undefined，直接调用会让首屏渲染抛错并白屏。
// 这里保留原生实现，同时提供基于 `crypto.getRandomValues` 的等价降级。
export function newId(): string {
  const source = globalThis.crypto
  if (source && typeof source.randomUUID === 'function') return source.randomUUID()

  const bytes = new Uint8Array(16)
  if (source && typeof source.getRandomValues === 'function') {
    source.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0'))
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
}
