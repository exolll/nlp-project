import { useEffect, useMemo, useState } from 'react'
import type { TaskSummary } from '../adapters/task'
import { groupTasks } from '../history'

export default function HistoryDrawer({
  open, onClose, tasks, activeId, onSelect, onNew, onRename, deleteAvailable, deleteReason, onDelete, onNotice,
}: {
  open: boolean
  onClose: () => void
  tasks: TaskSummary[]
  activeId: string
  onSelect: (id: string) => void
  onNew: () => void
  onRename: (id: string, title: string) => void
  deleteAvailable: boolean
  deleteReason: string
  onDelete: (id: string) => void
  onNotice: (message: string) => void
}) {
  const [menuId, setMenuId] = useState<string | null>(null)
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const now = useMemo(() => new Date(), [open, tasks.length])
  const groups = useMemo(
    () => groupTasks(tasks.map(task => ({ ...task, created_at: task.createdAt })), now),
    [tasks, now],
  )

  useEffect(() => {
    if (!open) { setMenuId(null); setRenameId(null); setConfirmId(null); return }
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  useEffect(() => { if (!open) return; setMenuId(null) }, [activeId, open])

  function commitRename() {
    if (renameId === null) return
    const next = renameDraft.trim()
    if (next) onRename(renameId, next)
    setRenameId(null); setRenameDraft('')
  }

  return <>
    <div className={`drawer-backdrop${open ? ' open' : ''}`} onClick={onClose} aria-hidden="true" />
    <aside className={`drawer history-drawer${open ? ' open' : ''}`} role="dialog" aria-modal="true" aria-label="任务历史" aria-hidden={!open}>
      <div className="drawer-head">
        <div><span className="drawer-brand"><span className="drawer-logo" aria-hidden="true">P</span>ProjectPilot</span><h2>任务历史 <span className="history-count">{tasks.length}</span></h2></div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭任务历史">×</button>
      </div>
      <button type="button" className="primary drawer-new" onClick={onNew}>＋ 新建任务</button>
      <div className="drawer-scroll">
        {!tasks.length
          ? <div className="history-empty">还没有任务。提交第一份材料后，记录会保存在当前会话中。</div>
          : groups.map(group => <section className="history-group" key={group.group}>
              <h4>{group.label}</h4>
              {group.tasks.map(task => <div className={`history-row-wrap${activeId === task.id ? ' active' : ''}`} key={task.id}>
                {renameId === task.id
                  ? <div className="history-rename">
                      <input
                        autoFocus value={renameDraft} maxLength={80} aria-label="新的任务标题"
                        onChange={event => setRenameDraft(event.target.value)}
                        onKeyDown={event => {
                          if (event.key === 'Enter') commitRename()
                          if (event.key === 'Escape') { setRenameId(null); setRenameDraft('') }
                        }}
                        onBlur={commitRename}
                      />
                      <button type="button" className="secondary" onMouseDown={event => event.preventDefault()} onClick={commitRename}>保存</button>
                    </div>
                  : <>
                      <button type="button" className="history-row" onClick={() => { onNotice(''); onSelect(task.id) }} title={task.title}>
                        <span className="task-icon" aria-hidden="true">▤</span>
                        <span className="task-name"><strong>{task.title}</strong><small>{new Date(task.createdAt).toLocaleString('zh-CN')}</small></span>
                        <span className={`task-status ${task.status}`}>{task.statusLabel}</span>
                      </button>
                      <button
                        type="button" className="row-menu-button" aria-label={`${task.title} 的更多操作`} aria-expanded={menuId === task.id}
                        onClick={() => { setMenuId(menuId === task.id ? null : task.id); setConfirmId(null) }}
                      >⋯</button>
                      {menuId === task.id && <div className="row-menu" role="menu">
                        <button type="button" role="menuitem" onClick={() => { setRenameId(task.id); setRenameDraft(task.title); setMenuId(null) }}>重命名</button>
                        {deleteAvailable
                          ? confirmId === task.id
                            ? <div className="row-menu-confirm"><p>删除后不可恢复，确认删除该任务？</p><div><button type="button" onClick={() => { setConfirmId(null); setMenuId(null) }}>取消</button><button type="button" className="danger" onClick={() => { setConfirmId(null); setMenuId(null); onDelete(task.id) }}>确认删除</button></div></div>
                            : <button type="button" role="menuitem" className="danger" onClick={() => setConfirmId(task.id)}>删除任务</button>
                          : <div className="row-menu-pending" role="menuitem" aria-disabled="true" title={deleteReason}>删除任务<span>待后端接口支持</span></div>}
                      </div>}
                    </>}
              </div>)}
            </section>)}
        {tasks.length > 0 && <p className="history-note">历史来自服务端 GET /tasks；标题与阶段偏好只保存在本浏览器。重开浏览器会新建会话，旧会话任务可能无法访问。</p>}
      </div>
    </aside>
  </>
}