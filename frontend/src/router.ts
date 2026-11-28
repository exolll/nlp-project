export type SettingsTab = 'api' | 'models' | 'ui'

export type Route =
  | { kind: 'home' }
  | { kind: 'task'; taskId: string; stage: number | null }
  | { kind: 'settings'; tab: SettingsTab }

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const stageNumbers = [1, 2, 3, 4, 5, 6]

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('?')[0].split('/').filter(Boolean)
  if (parts[0] === 'task' && UUID.test(parts[1] ?? '')) {
    const stage = parts[2] === 'stage' ? Number(parts[3]) : NaN
    return { kind: 'task', taskId: parts[1], stage: stageNumbers.includes(stage) ? stage : null }
  }
  if (parts[0] === 'settings') {
    return { kind: 'settings', tab: parts[1] === 'models' || parts[1] === 'ui' ? parts[1] : 'api' }
  }
  return { kind: 'home' }
}

export function routeHash(route: Route): string {
  if (route.kind === 'task') return `#/task/${route.taskId}${route.stage ? `/stage/${route.stage}` : ''}`
  if (route.kind === 'settings') return `#/settings/${route.tab}`
  return '#/'
}

export function sameRoute(a: Route, b: Route): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'task' && b.kind === 'task') return a.taskId === b.taskId && a.stage === b.stage
  if (a.kind === 'settings' && b.kind === 'settings') return a.tab === b.tab
  return true
}