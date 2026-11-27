import type { ReactNode } from 'react'
import type { SaveStatus } from '../autosave'
import type { StageView } from '../adapters/stages'
import StageNav from './StageNav'

export type Connection = 'connecting' | 'ready' | 'error'

const saveLabels: Record<SaveStatus, string> = {
  idle: '自动保存', saving: '保存中', saved: '已保存', failed: '保存失败',
}
const saveNotes: Record<SaveStatus, string> = {
  idle: '标题修改会在约 1 秒后保存到本浏览器',
  saving: '正在写入浏览器本地存储',
  saved: '已保存到本浏览器；分析结果始终以服务端为准',
  failed: '保存失败，点击重试',
}

const MenuIcon = () => <svg width="17" height="17" viewBox="0 0 16 16" aria-hidden="true">
  <path d="M2 4h12M2 8h12M2 12h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
</svg>

const GearIcon = () => <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <circle cx="12" cy="12" r="3.4" stroke="currentColor" strokeWidth="1.6" />
  <path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
</svg>

export default function AppShell({
  mode, title, titlePlaceholder, onTitleChange, onTitleCancel, saveStatus, saveMessage, onRetrySave,
  connection, stages, viewingStage, onSelectStage, onOpenHistory, onOpenSettings, onOpenApi, mock,
  onRestoreRetry, restoreNotice, children,
}: {
  mode: 'home' | 'task'
  title: string
  titlePlaceholder: string
  onTitleChange: (value: string) => void
  onTitleCancel: () => void
  saveStatus: SaveStatus
  saveMessage: string
  onRetrySave: () => void
  connection: Connection
  stages: StageView[] | null
  viewingStage: number
  onSelectStage: (stage: StageView) => void
  onOpenHistory: () => void
  onOpenSettings: () => void
  onOpenApi: () => void
  mock: boolean
  onRestoreRetry?: () => void
  restoreNotice?: string
  children: ReactNode
}) {
  return <div className="app">
    <header className="shell-header">
      <div className="shell-row">
        <button type="button" className="icon-button" onClick={onOpenHistory} aria-label="打开任务历史" title="任务历史">
          <MenuIcon />
        </button>
        <a className="brand" href="#/"><span className="logo">P</span><span className="brand-name">ProjectPilot</span><span className="brand-sub">项目领航</span></a>
        <span className="shell-divider" aria-hidden="true" />
        {mode === 'task'
          ? <input
              className="shell-title" value={title} maxLength={80} spellCheck={false} aria-label="任务标题"
              placeholder={titlePlaceholder} title={title || titlePlaceholder}
              onChange={event => onTitleChange(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Enter') event.currentTarget.blur()
                if (event.key === 'Escape') { onTitleCancel(); event.currentTarget.blur() }
              }}
            />
          : <span className="shell-title placeholder">{titlePlaceholder}</span>}
        {mode === 'task' && <button
          type="button"
          className={`save-chip ${saveStatus}`}
          onClick={saveStatus === 'failed' ? onRetrySave : undefined}
          disabled={saveStatus !== 'failed'}
          title={saveMessage || saveNotes[saveStatus]}
          aria-live="polite"
        >
          {saveStatus === 'saving' && <span className="spinner" aria-hidden="true" />}
          {saveLabels[saveStatus]}{saveStatus === 'failed' ? ' · 重试' : ''}
        </button>}
        <span className="shell-spacer" />
        {mock && <span className="mock-chip" title="当前前端连接的是仅开发/测试用的本地 Mock 服务">演示数据（Mock）</span>}
        <button type="button" className={`api-chip ${connection}`} onClick={onOpenApi} title="打开 API 配置">
          <i aria-hidden="true" />{connection === 'ready' ? 'API 已连接' : connection === 'connecting' ? '连接中…' : '连接失败'}
        </button>
        <button type="button" className="icon-button" onClick={onOpenSettings} aria-label="打开设置" title="设置">
          <GearIcon />
        </button>
      </div>
      {stages && <StageNav stages={stages} viewing={viewingStage} onSelect={onSelectStage} interactive={mode === 'task'} />}
      {restoreNotice && <div className="restore-bar" role="alert"><span>{restoreNotice}</span>{onRestoreRetry && <button type="button" className="text-button" onClick={onRestoreRetry}>重试恢复</button>}</div>}
    </header>
    <main className="shell-main" data-stage={mode === 'task' ? viewingStage : 'home'}>
      {children}
    </main>
  </div>
}