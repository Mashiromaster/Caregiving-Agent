/** FallGuard tools for the official DeepSeek Harness Web profile. */
import { spawn } from 'node:child_process'
import { createServer, request as httpRequest } from 'node:http'
import { randomBytes, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

export const name = 'caregiving-fallguard'
export const inject = ['tools', 'systemPrompt', 'webServer', 'agents', 'connection']

const fallguardDir = resolve(dirname(fileURLToPath(import.meta.url)), '../fall_guard')
let endpoint = ''
let internalToken = ''
let lastAgentId = null

async function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolvePort(port))
    })
  })
}

async function waitForBackend(child, url, token) {
  const until = Date.now() + 15000
  while (Date.now() < until) {
    if (child.exitCode !== null) throw new Error(`FallGuard worker exited: ${child.exitCode}`)
    try {
      const response = await fetch(`${url}/api/status`, {
        headers: { 'x-fallguard-token': token }, signal: AbortSignal.timeout(700),
      })
      if (response.ok) return
    } catch { /* worker may still be importing OpenCV */ }
    await new Promise(done => setTimeout(done, 250))
  }
  throw new Error('FallGuard worker did not become ready')
}

function proxyPanel(req, res, port, token, connection) {
  const rejection = connection.requestRejection(req)
  if (rejection !== undefined) {
    res.writeHead(rejection).end()
    return
  }
  const url = new URL(req.url, 'http://localhost')
  if (url.pathname === '/caregiving') {
    res.writeHead(302, { location: '/caregiving/' })
    res.end()
    return
  }
  const path = url.pathname.slice('/caregiving'.length) + url.search
  const headers = { ...req.headers, host: `127.0.0.1:${port}`,
    'x-fallguard-token': token }
  delete headers.connection
  delete headers.cookie
  delete headers.authorization
  const upstream = httpRequest({ hostname: '127.0.0.1', port, path,
    method: req.method, headers }, response => {
    res.writeHead(response.statusCode || 502, response.headers)
    response.pipe(res)
  })
  upstream.on('error', error => {
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' })
    res.end(`FallGuard worker unavailable: ${error.message}`)
  })
  req.pipe(upstream)
}

async function request(path, options = {}, signal) {
  const response = await fetch(`${endpoint}${path}`, {
    ...options,
    headers: { 'content-type': 'application/json', 'x-fallguard-token': internalToken },
    signal,
  })
  const data = await response.json()
  if (!response.ok || data.ok === false) {
    return { status: 'error', summary: data.reason || data.error || `HTTP ${response.status}`, data }
  }
  return { status: 'success', summary: '操作完成', data }
}

function register(ctx, { name, description, parameters, run }) {
  const properties = Object.fromEntries(Object.entries(parameters).map(([key, spec]) => {
    const { required, ...schema } = spec
    return [key, schema]
  }))
  const required = Object.entries(parameters).filter(([_key, spec]) => spec.required).map(([key]) => key)
  ctx.tools.register({
    name,
    description,
    parameters: { type: 'object', properties, required, additionalProperties: false },
    output: {
      schema: {
        type: 'object',
        properties: {
          status: { type: 'string' },
          summary: { type: 'string' },
          data: { type: 'object', additionalProperties: true },
        },
        required: ['status', 'summary', 'data'],
        additionalProperties: false,
      },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args, exec) {
      if (exec.agent?.id) lastAgentId = exec.agent.id
      try {
        return await run(args, exec.signal)
      } catch (error) {
        if (exec.signal.aborted) throw error
        return { status: 'error', summary: String(error), data: {} }
      }
    },
  })
}

export async function apply(ctx) {
  const port = await freePort()
  const python = process.env.FALLGUARD_PYTHON || 'python3.13'
  const token = randomBytes(32).toString('hex')
  const child = spawn(python, ['main.py', 'web', '--host', '127.0.0.1',
    '--port', String(port), '--no-browser'], {
    cwd: fallguardDir, stdio: 'inherit',
    env: { ...process.env, FALLGUARD_INTERNAL_TOKEN: token,
      FALLGUARD_DSH_MODE: '1' },
  })
  let spawnError
  child.on('error', error => { spawnError = error })
  ctx.effect(() => () => {
    if (child.exitCode === null) child.kill('SIGINT')
  })
  endpoint = `http://127.0.0.1:${port}`
  internalToken = token
  try {
    await waitForBackend(child, endpoint, token)
  } catch (error) {
    if (child.exitCode === null) child.kill('SIGINT')
    throw spawnError || error
  }
  ctx.effect(() => ctx.webServer.register({
    kind: 'prefix', path: '/caregiving',
    handler: (req, res) => proxyPanel(req, res, port, token, ctx.connection),
  }))
  const seen = new Set()
  const recent = await request('/api/events?limit=50')
  for (const row of recent.data?.events || []) seen.add(row.event_id)
  let active = true
  let checking = false
  const timer = setInterval(async () => {
    if (!active || checking) return
    checking = true
    try {
      const result = await request('/api/events?limit=50')
      if (result.status !== 'success' || !active) return
      const rows = result.data.events || []
      for (const row of [...rows].reverse()) {
        if (!row.event_id || seen.has(row.event_id)) continue
        seen.add(row.event_id)
        const agent = lastAgentId && ctx.agents.get(lastAgentId)
        if (!agent) continue
        const summary = {
          event_id: row.event_id, event_type: row.event_type,
          level: row.level, outcome: row.outcome, detected_at: row.detected_at,
          calls_ok: row.calls_ok, agent_mode: row.agent_mode,
        }
        try {
          agent.followup({
            id: randomUUID(), role: 'user', source: { kind: 'caregiving-event' },
            content: [{ type: 'text', text: `守望者监测刚归档新事件：${JSON.stringify(summary)}。请核对事件记录并向用户简洁报告，勿把外呼受理误说成接通。` }],
          })
        } catch (error) { console.warn('[caregiving] event delivery failed:', error) }
      }
      if (seen.size > 500) {
        seen.clear()
        for (const row of rows) seen.add(row.event_id)
      }
    } catch { /* backend may be restarting */ }
    finally { checking = false }
  }, 3000)
  ctx.effect(() => () => { active = false; clearInterval(timer) })
  ctx.systemPrompt.section({
    name: 'caregiving:fallguard',
    order: 120,
    interpolate: false,
    text: `你是守望者看护助手。用中文简洁回答。管理摄像头监测时使用 fallguard_* 工具；不要把模型推断当作已经检测到的事件。监控与配置页在 /caregiving/，与对话界面由同一个 DSH Host 提供。
跌倒检测与外呼由 FallGuard 运行时执行：事件成立后按已保存配置留证、给取消机会，再按联系人升级链外呼。启动前可用 fallguard_status 告知用户当前通道是否为演练模式、是否配置联系人。只有用户明确要求真实拨号时才用 fallguard_call_mode 开启真实拨号；配置变更后需重启监测。
用户要求定时开启、停止或查询监测时，使用 Harness 自带的 schedule_create / schedule_list / schedule_view / schedule_edit 工具；定时指令应明确写出到点调用 fallguard_start 或 fallguard_stop。定时任务只有 Harness Host 运行时才会触发。
不要自行声称电话已接通：外呼通道返回的“提交成功”只表示请求被平台受理。`,
  })
  register(ctx, {
    name: 'fallguard_status',
    description: '查询跌倒监测插件的运行状态、外呼配置、当前事件及最近告警。涉及当前状态时先调用此工具。',
    parameters: {},
    async run(_args, signal) {
      const result = await request('/api/status', {}, signal)
      if (result.status === 'success') result.summary = result.data.state === 'running' ? '监测正在运行' : '监测未运行'
      return result
    },
  })

  register(ctx, {
    name: 'fallguard_start',
    description: '启动摄像头跌倒监测插件。检测到事件后，Python Runtime 会独立执行取消窗口、留证和自动外呼。source 留空使用已保存配置；demo 是合成视频演练。不要为了回答普通问题启动监测。',
    parameters: { source: { type: 'string', description: '可选：demo 或摄像头编号；默认使用配置' } },
    async run(args, signal) {
      const result = await request('/api/run/start', {
        method: 'POST', body: JSON.stringify({ source: args.source || '' }),
      }, signal)
      if (result.status === 'success') result.summary = '已启动跌倒监测'
      return result
    },
  })

  register(ctx, {
    name: 'fallguard_stop',
    description: '停止跌倒监测。运行时会先等待正在处理的事件和留证收尾。仅在用户明确要求停止时调用。',
    parameters: {},
    async run(_args, signal) {
      const result = await request('/api/run/stop', { method: 'POST', body: '{}' }, signal)
      if (result.status === 'success') result.summary = result.data.pending ? '已请求停止，正在收尾' : '监测已停止'
      return result
    },
  })

  register(ctx, {
    name: 'fallguard_cancel_alert',
    description: '在当前事件的取消窗口内撤销待发外呼。只有用户明确要求取消当前告警时调用；窗口结束后会返回错误。',
    parameters: {},
    async run(_args, signal) {
      const status = await request('/api/status', {}, signal)
      if (status.status !== 'success') return status
      if (!status.data.active?.cancellable) {
        return { status: 'error', summary: '当前没有可取消的告警窗口', data: {} }
      }
      const result = await request('/api/run/key', {
        method: 'POST', body: JSON.stringify({ key: 'c' }),
      }, signal)
      if (result.status === 'success') result.summary = '已发送取消指令，请查询状态确认事件已取消'
      return result
    },
  })

  register(ctx, {
    name: 'fallguard_events',
    description: '读取最近的跌倒或求助事件摘要及处置结果。可用 limit 限定条数。',
    parameters: { limit: { type: 'integer', description: '返回条数，1 到 50' } },
    async run(args, signal) {
      const limit = Math.max(1, Math.min(50, Number(args.limit) || 10))
      const result = await request(`/api/events?limit=${limit}`, {}, signal)
      if (result.status === 'success') result.summary = `读取 ${result.data.events?.length || 0} 条事件`
      return result
    },
  })

  register(ctx, {
    name: 'fallguard_call_mode',
    description: '切换电话外呼演练模式。只有用户明确要求真实拨号时才传 real=true；这会在下一次事件成立时产生实际呼叫及费用。更改运行中的配置后必须停止并重新启动监测。',
    parameters: { real: { type: 'boolean', description: 'true 为真实拨号，false 为演练', required: true } },
    async run(args, signal) {
      if (typeof args.real !== 'boolean') return { status: 'error', summary: 'real 必须是布尔值', data: {} }
      const status = await request('/api/status', {}, signal)
      if (status.status !== 'success') return status
      const config = status.data.config || {}
      if (args.real && (config.provider === 'none' || !config.contacts)) {
        return { status: 'error', summary: '先在 FallGuard 面板配置外呼通道与联系人', data: {} }
      }
      const result = await request('/api/config', {
        method: 'POST',
        body: JSON.stringify({ values: { call: { dry_run: !args.real } } }),
      }, signal)
      if (result.status !== 'success') return result
      return {
        status: 'success',
        summary: args.real ? '已保存真实拨号模式' : '已保存演练模式',
        data: { real: args.real, restart_required: status.data.state === 'running' },
      }
    },
  })
}
