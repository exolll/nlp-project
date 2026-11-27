import { useEffect, useState } from 'react'
import type { Config } from '../api'
import { capabilities } from '../capabilities'
import type { Connection } from './AppShell'

export type SettingsTab = 'api' | 'models' | 'ui'

export interface ProbeResult { ok: boolean; latencyMs: number; message: string }

const tabs: { key: SettingsTab; label: string; hint: string }[] = [
  { key: 'api', label: 'API 配置', hint: '连接与鉴权' },
  { key: 'models', label: '模型参数', hint: '只读展示' },
  { key: 'ui', label: '界面偏好', hint: '本浏览器' },
]

const Pending = ({ text = '待后端接口支持' }: { text?: string }) => <span className="pending-tag" title={capabilities.llmConfig.reason}>{text}</span>

export default function SettingsDrawer({
  open, tab, onTab, onClose, connection, config, tokenPresent, mock, onProbe,
}: {
  open: boolean
  tab: SettingsTab
  onTab: (tab: SettingsTab) => void
  onClose: () => void
  connection: Connection
  config: Config | null
  tokenPresent: boolean
  mock: boolean
  onProbe: () => Promise<ProbeResult>
}) {
  const [probe, setProbe] = useState<ProbeResult | null>(null)
  const [probing, setProbing] = useState(false)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  async function runProbe() {
    setProbing(true)
    try { setProbe(await onProbe()) }
    finally { setProbing(false) }
  }

  return <>
    <div className={`drawer-backdrop${open ? ' open' : ''}`} onClick={onClose} aria-hidden="true" />
    <aside className={`drawer settings-drawer${open ? ' open' : ''}`} role="dialog" aria-modal="true" aria-label="设置" aria-hidden={!open}>
      <div className="drawer-head">
        <div><span className="drawer-eyebrow">SETTINGS</span><h2>设置</h2></div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭设置">×</button>
      </div>
      <div className="settings-body">
        <nav className="settings-menu" aria-label="设置模块">
          {tabs.map(item => <button
            type="button" key={item.key} className={tab === item.key ? 'active' : ''}
            aria-current={tab === item.key ? 'page' : undefined} onClick={() => onTab(item.key)}
          >
            <strong>{item.label}</strong><small>{item.hint}</small>
          </button>)}
        </nav>
        <div className="settings-content">
          {tab === 'api' && <>
            <h3>API 连接</h3>
            <dl className="settings-list">
              <dt>连接状态</dt><dd>{connection === 'ready' ? '已连接（本次会话）' : connection === 'connecting' ? '正在连接…' : '未连接或连接失败'}</dd>
              <dt>请求地址</dt><dd><code>/api/v1</code>（同源；由 Vite 代理到后端{mock ? '，当前开发模式指向本地 Mock' : ''}）</dd>
              <dt>鉴权方式</dt><dd>Bearer 会话令牌 · {tokenPresent ? '本次会话令牌已存在' : '暂无会话令牌'}</dd>
              <dt>供应商</dt><dd>DeepSeek（由后端固定）<Pending /></dd>
              <dt>Base URL / 模型</dt><dd>后端未提供读取接口，前端不猜测当前值<Pending text="待接口支持" /></dd>
              <dt>API Key</dt><dd>仅保存在后端环境变量，前端不读取、不缓存明文<Pending text="待接口支持" /></dd>
            </dl>
            <div className="settings-actions">
              <button type="button" className="secondary" disabled={probing} onClick={() => void runProbe()}>{probing ? '正在检测…' : '检测后端可达性'}</button>
              <button type="button" className="secondary" disabled title={capabilities.llmTest.reason}>连接测试<Pending /></button>
            </div>
            {probe && <p className={probe.ok ? 'verification-ok' : 'warning'} role="status">{probe.message}{probe.ok ? ` · 耗时 ${probe.latencyMs} ms` : ''}</p>}
            <p className="muted">「检测后端可达性」只执行一次无副作用的 GET /config；后端可达不代表模型密钥有效。保存供应商 / Base URL / 模型 / API Key 的接口尚未提供，本页不会发送任何配置修改。</p>
            {config && <div className="settings-block">
              <h4>服务限制（真实读取 GET /config）</h4>
              <dl className="settings-list">
                <dt>单文件上限</dt><dd>{(config.max_file_bytes / 1_000_000).toFixed(1)} MB</dd>
                <dt>PDF 页数上限</dt><dd>{config.max_pdf_pages} 页</dd>
                <dt>主输入文本上限</dt><dd>{config.max_primary_characters.toLocaleString()} 字符</dd>
                <dt>补充文本上限</dt><dd>{config.max_supplement_characters.toLocaleString()} 字符</dd>
                <dt>存储后端 / 环境</dt><dd>{config.storage_backend} / {config.environment}</dd>
              </dl>
            </div>}
            {mock && <p className="warning">当前为本地 Mock 服务：以上数值与数据均为演示用途，不代表真实后端。</p>}
          </>}
          {tab === 'models' && <>
            <h3>模型参数</h3>
            <p className="settings-note">后端尚未提供生效配置的只读接口，本页不显示推测值，也不提供无法生效的输入框。</p>
            <dl className="settings-list">
              <dt>可调参数</dt><dd>无。模型、温度、最大输出与用量上限均由后端固定<Pending text="待接口支持" /></dd>
              <dt>当前模型</dt><dd>尚无真实调用记录；提取/搜索响应中的模型信息会在对应阶段显示<Pending text="待接口支持" /></dd>
            </dl>
            <p className="muted">后续接口就绪后，这里将只读展示 provider、model、embedding 模型与是否启用搜索；可调项另行确认后再加入。</p>
          </>}
          {tab === 'ui' && <>
            <h3>界面偏好</h3>
            <dl className="settings-list">
              <dt>主题</dt><dd>浅色（固定）</dd>
              <dt>语言</dt><dd>简体中文（固定）</dd>
              <dt>动效</dt><dd>自动遵循系统「减少动态效果」设置</dd>
              <dt>标题与阶段</dt><dd>保存在本浏览器（localStorage）；约 1 秒防抖自动保存</dd>
              <dt>任务历史</dt><dd>始终来自服务端 GET /tasks，不使用本地缓存结果</dd>
            </dl>
            <p className="muted">界面偏好只影响显示，不缓存任何分析结果；移除浏览器数据会丢失标题与上次阶段，但服务端任务不受影响。</p>
          </>}
        </div>
      </div>
    </aside>
  </>
}