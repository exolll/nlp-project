import { useEffect } from 'react'

export default function Toast({ message, onClose }: { message: string; onClose: () => void }) {
  useEffect(() => {
    if (!message) return
    const timer = setTimeout(onClose, 5000)
    return () => clearTimeout(timer)
  }, [message])
  if (!message) return null
  return <div className="toast" role="status">
    <span>{message}</span>
    <button type="button" aria-label="关闭提示" onClick={onClose}>×</button>
  </div>
}