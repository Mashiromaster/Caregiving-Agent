'use strict';
const API_BASE = location.pathname.startsWith('/caregiving/') ? '/caregiving' : '';
const apiPath = (path) => API_BASE + path;
/* 守望者 FallGuard 控制台前端。
   无构建步骤、无依赖——这是个单机运维面板，加 npm 工具链只会让部署变难。
   状态存在 S 里，每个页面是一个 render 函数，轮询拿 status 后只刷新需要变的部分。 */

const S = {
  tab: 'overview',
  status: null,
  schema: null,
  config: null,     // 服务端读到的原始配置
  draft: null,      // 表单当前值
  errors: {},
  warnings: [],
  dirty: false,
  events: null,
  eventType: '',
  eventDetail: null,
  doctor: null,
  doctorLoading: false,
  logs: [],
  logSeq: 0,
  callTest: null,
  streamOn: false,
};

const $ = (sel, root) => (root || document).querySelector(sel);
const el = (tag, attrs, kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of [].concat(kids || [])) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
  }
  return node;
};

async function api(path, options) {
  const res = await fetch(apiPath(path), Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, options || {}));
  let body = null;
  try { body = await res.json(); } catch (_e) { body = {}; }
  return { ok: res.ok, status: res.status, body };
}

const post = (path, data) =>
  api(path, { method: 'POST', body: JSON.stringify(data || {}) });

function toast(message, kind) {
  const node = el('div', { class: `toast toast-${kind || 'info'}`, text: message });
  $('#toasts').appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s';
    setTimeout(() => node.remove(), 250);
  }, kind === 'bad' ? 7000 : 4000);
}

const fmtDur = (s) => {
  if (s === null || s === undefined) return '—';
  const n = Math.floor(s);
  if (n < 60) return `${n}秒`;
  if (n < 3600) return `${Math.floor(n / 60)}分${n % 60}秒`;
  return `${Math.floor(n / 3600)}时${Math.floor((n % 3600) / 60)}分`;
};

const shortTime = (iso) => (iso || '').replace('T', ' ').slice(0, 19);

// 事件类型 → 中文。插件产出的 kind 是代码标识，值班台看不懂
const TYPE_LABEL = {
  fall: '跌倒',
  risk_warning: '风险预警',
  manual_test: '人工演练',
};
const typeName = (t) => TYPE_LABEL[t] || t || '未知';

// 时间线的 stage 是代码内部名，值班台看不懂，这里翻一层
const STAGE_LABEL = {
  detected: '判定成立',
  identified: '身份识别',
  graded: '规则分级',
  escalated: '等级提升',
  risk_assessed: '风险评估',
  evidence_captured: '留证完成',
  local_alert: '本地告警（响铃 + 播报）',
  cancelled_by_operator: '人工取消',
  call_suppressed: '命中抑制时段，不外呼',
  agent_start: '智能体接手处置',
  agent_done: '处置结束',
};

/* ------------------------------------------------------------------ 布局 */
function setTab(tab) {
  S.tab = tab;
  for (const btn of document.querySelectorAll('#nav button')) {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  }
  $('#page-title').textContent = {
    overview: '总览', live: '实时监控', events: '事件记录',
    config: '配置', doctor: '体检',
  }[tab];
  S.streamOn = false;
  render();
  if (tab === 'events' && !S.events) loadEvents();
  if (tab === 'doctor' && !S.doctor) loadDoctor();
  if (tab === 'config' && !S.schema) loadConfig();
}

function render() {
  const page = $('#page');
  page.innerHTML = '';
  const draw = {
    overview: renderOverview, live: renderLive, events: renderEvents,
    config: renderConfig, doctor: renderDoctor,
  }[S.tab];
  draw(page);
}

/* ------------------------------------------------------------------ 总览 */
function runStatePill(st) {
  if (!st) return el('span', { class: 'pill pill-idle', text: '连接中' });
  const map = {
    running: ['pill-live', '监测中'], starting: ['pill-info', '启动中'],
    stopping: ['pill-warn', '停止中'], stopped: ['pill-idle', '已停止'],
    idle: ['pill-idle', '未运行'],
  };
  const [cls, text] = map[st.state] || ['pill-idle', st.state];
  return el('span', { class: `pill ${cls}`, text });
}

function renderOverview(root) {
  const st = S.status;
  if (!st) { root.appendChild(el('div', { class: 'loading', text: '加载中…' })); return; }

  const cfg = st.config;
  const stats = st.stats || {};
  const es = st.event_stats || {};

  if (st.config_error) {
    root.appendChild(el('div', { class: 'card' }, [
      notice('bad', `配置加载失败：${st.config_error}`),
    ]));
  }

  root.appendChild(el('div', { class: 'stat-row' }, [
    stat('本次运行事件', stats.events || 0, `已外呼 ${stats.calls || 0} 通`),
    stat('累计事件', es.total ?? '—',
         es.total !== undefined ? `外呼 ${es.called}　取消 ${es.cancelled}` : '尚无记录'),
    stat('标注误报', es.labelled_false_alarm ?? '—',
         es.unlabelled !== undefined ? `${es.unlabelled} 起未标注` : ''),
    stat('处理帧数', stats.frames || 0,
         st.fps ? `${st.fps} fps · 运行 ${fmtDur(st.uptime)}` : '未运行'),
  ]));

  // 配置没做完的地方要在首页说清楚，否则用户以为「启动了就会打电话」
  const blockers = [];
  if (cfg) {
    if (!cfg.saved) blockers.push(['warn',
      '尚未保存过配置，当前跑的是示例默认值。去「配置」页填写场景与联系人后保存。']);
    if (!cfg.contacts) blockers.push(['bad',
      '没有配置联系人——判定成立后系统无人可呼，整条处置链路会停在「记录」这一步。']);
    if (cfg.provider === 'none') blockers.push(['warn',
      '外呼通道为 none，只会记录事件不会拨号。']);
    else if (cfg.dry_run) blockers.push(['info',
      '演练模式开启中：会走完整链路但只打印请求参数，不真实拨号。联调通过后在「配置 → 外呼」里关闭。']);
    else blockers.push(['ok',
      '真实拨号已启用。下一次判定成立会实际打出电话并产生费用。']);
    if (cfg.dsh_mode) blockers.push(['info',
      '由 DeepSeek Harness 负责对话与定时任务；跌倒告警按确定性规则处置，模型离线时仍可运行。']);
    else if (cfg.agent_enabled && !cfg.agent_key) blockers.push(['warn',
      '智能体已启用但未提供 api_key，会降级为确定性规则处置（兜底外呼仍然生效）。']);
    if (!cfg.cancel_seconds) blockers.push(['warn',
      '取消窗口为 0，误报没有人工撤销的机会。']);
  }

  root.appendChild(el('div', { class: 'grid-2' }, [
    el('div', { class: 'card' }, [
      el('h2', { text: '当前配置' }),
      cfg ? el('dl', { class: 'kv' }, [
        el('dt', { text: '场景' }), el('dd', { text: `${cfg.place} · ${cfg.resident}` }),
        el('dt', { text: '视频源' }), el('dd', { text: String(cfg.source) }),
        el('dt', { text: '感知插件' }),
        el('dd', { text: (cfg.sensors || []).join(' / ') || '无' }),
        el('dt', { text: '姿态后端' }),
        el('dd', { text: st.backend ? `${cfg.backend}（实际 ${st.backend}）` : cfg.backend }),
        el('dt', { text: '外部设备' }),
        el('dd', { text: cfg.devices ? `${cfg.devices} 个` : '未接入（纯视觉）' }),
        el('dt', { text: '身份识别' }),
        el('dd', { text: (cfg.identity || []).join(' → ') || '无' }),
        el('dt', { text: '外呼' }),
        el('dd', { text: st.gateway || `${cfg.provider} / ${cfg.dry_run ? '演练' : '真实拨号'} / ${cfg.contacts} 位联系人` }),
        el('dt', { text: '取消窗口' }), el('dd', { text: `${cfg.cancel_seconds} 秒` }),
        el('dt', { text: '配置文件' }), el('dd', { class: 'mono', text: cfg.path }),
      ]) : el('div', { class: 'empty', text: '配置不可用' }),
    ]),
    el('div', { class: 'card' }, [
      el('h2', { text: '就绪状态' }),
      el('p', { class: 'card-desc', text: '判定成立后会依次走这条链路，任一环节缺配置都会在下面列出。' }),
      el('div', { class: 'chain' }, [
        el('span', { class: 'step', text: '感知插件' }), el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: '身份' }), el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: '记忆' }), el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: '规则分级' }), el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: '留证' }), el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: `取消窗 ${cfg ? cfg.cancel_seconds : '?'}s` }),
        el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: cfg?.dsh_mode ? '规则处置' : '智能体' }),
        el('span', { class: 'arrow', text: '→' }),
        el('span', { class: 'step', text: '外呼' }),
      ]),
      el('p', { class: 'card-desc', style: 'margin:10px 0 0',
        text: '风险预警是与之并行的独立通路：只记录与提示，不外呼。' }),
      el('div', { style: 'margin-top:14px' },
         blockers.map(([kind, text]) => notice(kind, text))),
    ]),
  ]));

  if (st.risk || (st.devices || []).length) root.appendChild(riskCard(st));
  if (st.active) root.appendChild(activeCard(st.active));
}

function riskCard(st) {
  const risk = st.risk;
  const devices = st.devices || [];
  const levelMap = { normal: ['pill-ok', '风险可控'], warn: ['pill-warn', '需要关注'],
                     elevated: ['pill-bad', '风险偏高'] };
  const [cls, label] = risk ? (levelMap[risk.level] || ['pill-idle', risk.level])
                            : ['pill-idle', '未评估'];
  return el('div', { class: 'card' }, [
    el('div', { style: 'display:flex;gap:10px;align-items:center' }, [
      el('h2', { text: '风险预警', style: 'margin:0' }),
      el('span', { class: `pill ${cls}`, text: label }),
      risk ? el('span', { class: 'card-desc', style: 'margin:0',
                          text: `${risk.score} 分` }) : null,
    ]),
    el('p', { class: 'card-desc', style: 'margin:8px 0 12px',
      text: '汇总步态、心率等连续信号预测风险。越阈只记录与提示，**不会外呼**——'
            + '它与跌倒告警是两条独立通路，互不影响。' }),
    risk && risk.factors.length
      ? el('table', {}, [
          el('thead', {}, el('tr', {}, ['因子', '得分', '依据']
            .map((h) => el('th', { text: h })))),
          el('tbody', {}, risk.factors.map((f) => el('tr', {}, [
            el('td', { text: f.label }),
            el('td', { class: 'num', text: String(f.points) }),
            el('td', { class: 'card-desc', style: 'margin:0', text: f.reason }),
          ]))),
        ])
      : el('div', { class: 'empty', text: risk ? '各项信号正常。' : '运行后显示风险评分' }),
    devices.length ? el('div', { style: 'margin-top:14px' }, [
      el('h2', { text: '设备', style: 'margin-bottom:8px' }),
      el('div', { class: 'feat' }, devices.map((d) => el('div', {}, [
        el('div', { class: 'k', text: `${d.name}${d.simulated ? '（模拟）' : ''}` }),
        el('div', { class: `v ${d.online ? '' : 'hot'}`,
                    text: d.online ? '在线' : '离线' }),
      ]))),
    ]) : null,
  ]);
}

function stat(k, v, s) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'k', text: k }),
    el('div', { class: 'v', text: String(v) }),
    s ? el('div', { class: 's', text: s }) : null,
  ]);
}

function notice(kind, text) {
  const ico = { ok: '✓', warn: '!', bad: '✕', info: 'i' }[kind] || 'i';
  return el('div', { class: `notice notice-${kind}` }, [
    el('span', { class: 'ico', text: ico }),
    el('span', { text }),
  ]);
}

function activeCard(active) {
  const calls = active.calls || [];
  return el('div', { class: 'card' }, [
    el('h2', { text: '当前事件' }),
    el('dl', { class: 'kv' }, [
      el('dt', { text: '事件号' }), el('dd', { class: 'mono', text: active.event_id }),
      el('dt', { text: '判定' }),
      el('dd', { text: `${active.level} · ${active.trigger} · 置信度 ${active.confidence}` }),
      el('dt', { text: '状态' }), el('dd', { text: active.banner || active.outcome }),
      el('dt', { text: '已过' }), el('dd', { text: fmtDur(active.started_ago) }),
    ]),
    calls.length ? el('div', { style: 'margin-top:12px' }, [
      el('table', {}, [
        el('thead', {}, el('tr', {}, [
          el('th', { text: '联系人' }), el('th', { text: '角色' }),
          el('th', { text: '号码' }), el('th', { text: '结果' }), el('th', { text: '耗时' }),
        ])),
        el('tbody', {}, calls.map((c) => el('tr', {}, [
          el('td', { text: c.name }), el('td', { text: c.role }),
          el('td', { class: 'mono', text: c.phone }),
          el('td', {}, el('span', {
            class: `pill ${c.ok ? (c.dry_run ? 'pill-info' : 'pill-ok') : 'pill-bad'}`,
            text: `${c.code}${c.dry_run ? ' 演练' : ''}`,
          })),
          el('td', { class: 'num', text: `${c.latency_ms}ms` }),
        ]))),
      ]),
    ]) : null,
    active.notes && active.notes.length
      ? el('div', { style: 'margin-top:12px' },
           active.notes.slice(-6).map((n) => notice('info', n)))
      : null,
  ]);
}

/* ------------------------------------------------------------------ 实时 */
function renderLive(root) {
  const st = S.status || {};
  const running = st.state === 'running';
  const active = st.active;

  root.appendChild(el('div', { class: 'card' }, [
    el('h2', { text: '启动选项' }),
    el('p', { class: 'card-desc',
      text: '这里的选项只影响本次运行，不写回配置文件。想长期生效请去「配置」页。' }),
    el('div', { class: 'launch' }, [
      el('div', {}, [
        el('label', { text: '视频源（留空用配置值）' }),
        el('input', { type: 'text', id: 'opt-source', placeholder: '0 / demo / screen / xx.mp4',
                      disabled: running }),
      ]),
      el('div', {}, [
        el('label', { text: '姿态后端' }),
        el('select', { id: 'opt-backend', disabled: running },
           ['', 'auto', 'mediapipe', 'yolo', 'motion'].map((v) =>
             el('option', { value: v, text: v || '用配置值' }))),
      ]),
      el('div', {}, [
        el('label', { text: '取消窗口覆盖（秒）' }),
        el('input', { type: 'number', id: 'opt-cancel', min: '0', step: '1',
                      placeholder: '留空用配置值', disabled: running }),
      ]),
      el('div', {}, [
        el('label', { text: '最长运行（秒）' }),
        el('input', { type: 'number', id: 'opt-max', min: '1', step: '10',
                      placeholder: '留空=一直跑', disabled: running }),
      ]),
      el('div', { class: 'checks' }, [
        checkbox('opt-nocall', '强制演练（不真实拨号）', running),
        checkbox('opt-noagent', '关闭 LLM，仅规则处置', running),
        checkbox('opt-quiet', '静音（不播报不响铃）', running),
      ]),
    ]),
  ]));

  root.appendChild(el('div', { class: 'live-grid' }, [
    el('div', {}, [
      el('div', { class: 'viewport', id: 'viewport' },
         running
           ? el('img', { id: 'stream', src: apiPath(`/api/stream.mjpg?t=${Date.now()}`), alt: '实时画面' })
           : el('div', { class: 'placeholder',
                text: '未在运行。点右上角「启动监测」后这里会显示带判定叠加层的实时画面。' })),
      el('div', { class: 'live-actions' }, [
        el('button', {
          class: 'btn btn-danger', disabled: !running || !active || !active.cancellable,
          onclick: () => sendKey('c'),
          text: '取消外呼（倒计时内有效）',
        }),
        el('button', {
          class: 'btn', disabled: !running || !st.last_event_id,
          onclick: () => sendKey('f'),
          text: '标注最近事件为误报',
        }),
        el('button', {
          class: 'btn', disabled: !running, onclick: () => sendKey('t'),
          text: '手动触发一次演练',
        }),
      ]),
      active ? activeCard(active) : null,
    ]),
    el('div', {}, [
      el('div', { class: 'card' }, [
        el('h2', { text: '判定特征' }),
        // 固定容器：逐帧读数由轮询增量替换它的内容。容器必须一直在，
        // 不能靠查找内部的 .feat——刚启动时面板里是"运行后显示读数"的
        // 占位符，没有 .feat 可查，面板会永远停在占位符上。
        el('div', { id: 'feat-panel' },
           featurePanel(st.observation || {}, S.status && S.status.config)),
      ]),
      el('div', { class: 'card' }, [
        el('h2', { text: '运行日志' }),
        el('div', { class: 'log', id: 'logbox' }),
      ]),
    ]),
  ]));

  paintLogs();
  const box = $('#logbox');
  if (box) box.scrollTop = box.scrollHeight;
}

function checkbox(id, label, disabled) {
  return el('label', { class: 'switch' }, [
    el('input', { type: 'checkbox', id, disabled }),
    el('span', { class: 'track' }),
    el('span', { class: 'txt', text: label }),
  ]);
}

function featurePanel(obs, cfg) {
  if (!obs || obs.state === undefined) {
    return el('div', { class: 'empty', text: '运行后这里显示逐帧判定读数' });
  }
  const stateMap = {
    idle: ['pill-idle', '空闲'], upright: ['pill-ok', '站立'],
    lying: ['pill-warn', '躺倒中'], fall: ['pill-bad', '跌倒'],
    cooldown: ['pill-info', '静默期'], zone_suppressed: ['pill-info', '排除区域'],
  };
  const [cls, text] = stateMap[obs.state] || ['pill-idle', obs.state || '—'];
  const cell = (k, v, hot) => el('div', {}, [
    el('div', { class: 'k', text: k }),
    el('div', { class: `v${hot ? ' hot' : ''}`, text: v }),
  ]);
  return el('div', {}, [
    el('div', { style: 'margin-bottom:10px' }, el('span', { class: `pill ${cls}`, text })),
    el('div', { class: 'feat' }, [
      cell('检测到人', obs.has_person ? `是 ${obs.person_score ?? ''}` : '否'),
      cell('躯干角', obs.torso_angle === null || obs.torso_angle === undefined
        ? 'n/a' : `${obs.torso_angle}°`, obs.torso_angle >= 60),
      cell('宽高比', obs.aspect ?? '—'),
      cell('下降速率', `${obs.descent_rate ?? 0}/s`, (obs.descent_rate || 0) >= 0.25),
      cell('体动能量', obs.motion ?? '—'),
      cell('躺倒时长', `${obs.lying_for ?? 0}s`, (obs.lying_for || 0) > 0),
    ]),
    el('p', { class: 'card-desc', style: 'margin:10px 0 0',
      text: '躯干角 ≥ 阈值且在地面高度 → 判为躺倒；此前若捕捉到足够快的下降，走高置信路径。' }),
  ]);
}

function paintLogs() {
  const box = $('#logbox');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = '';
  if (!S.logs.length) {
    box.appendChild(el('div', { class: 'log-empty', text: '暂无日志。' }));
    return;
  }
  for (const row of S.logs.slice(-300)) {
    let cls = '';
    if (/\[(判定|外呼失败|运行异常|启动失败|兜底)\]/.test(row.text)) cls = 'l-bad';
    else if (/\[(倒计时|抑制|标注|智能体)\]/.test(row.text)) cls = 'l-warn';
    else if (/\[(外呼|归档)\]/.test(row.text)) cls = 'l-ok';
    else if (/\[(启动|提示|网页|配置|停止)\]/.test(row.text)) cls = 'l-info';
    box.appendChild(el('div', {}, [
      el('span', { class: 't', text: row.at }),
      el('span', { class: cls, text: row.text }),
    ]));
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function sendKey(key) {
  const res = await post('/api/run/key', { key });
  if (!res.body.ok) toast(res.body.reason || '操作失败', 'warn');
  await refreshStatus();
}

/* ------------------------------------------------------------------ 事件 */
async function loadEvents() {
  const q = S.eventType ? `&type=${encodeURIComponent(S.eventType)}` : '';
  const res = await api(`/api/events?limit=200${q}`);
  S.events = res.body;
  if (S.tab === 'events') render();
}

function renderEvents(root) {
  if (!S.events) { root.appendChild(el('div', { class: 'loading', text: '加载中…' })); return; }
  const { events, stats } = S.events;
  const types = S.events.types || [];

  if (stats) {
    const byType = Object.entries(stats.by_type || {})
      .map(([k, v]) => `${typeName(k)} ${v}`).join('　');
    root.appendChild(el('div', { class: 'stat-row' }, [
      stat('累计事件', stats.total, byType),
      stat('已外呼', stats.called, ''),
      stat('人工取消', stats.cancelled, ''),
      stat('标注误报', stats.labelled_false_alarm, `${stats.unlabelled} 起未标注`),
    ]));
  }

  const card = el('div', { class: 'card' }, [
    el('div', { style: 'display:flex;justify-content:space-between;align-items:center' }, [
      el('h2', { text: '事件记录', style: 'margin:0' }),
      // 预警与告警混在一张表里，筛选是区分它们的主要手段
      types.length > 1 ? el('select', {
        style: 'width:auto;min-width:140px',
        onchange: (e) => { S.eventType = e.target.value; S.eventDetail = null; loadEvents(); },
      }, [el('option', { value: '', text: '全部类型',
                         selected: !S.eventType })].concat(
        types.map((t) => el('option', {
          value: t, text: typeName(t), selected: S.eventType === t })))) : null,
    ]),
    el('p', { class: 'card-desc',
      text: '标注写入独立的 labels.jsonl，events.jsonl 只追加不修改——原始记录始终可审计。'
            + '风险预警也在这里留痕（结果为「仅记录」），便于事后复盘阈值是否合适。' }),
  ]);

  if (!events.length) {
    card.appendChild(el('div', { class: 'empty',
      text: S.eventType ? `没有「${typeName(S.eventType)}」类型的事件。`
        : '暂无事件。可在「实时监控」页点「手动触发一次演练」走一遍完整链路。' }));
  } else {
    card.appendChild(el('table', {}, [
      el('thead', {}, el('tr', {}, ['事件号', '时间', '类型', '等级', '规则',
        '触发路径', '置信度', '结果', '外呼', '标注', ''].map((h) => el('th', { text: h })))),
      el('tbody', {}, events.map((e) => eventRow(e))),
    ]));
  }
  root.appendChild(card);

  if (S.eventDetail) root.appendChild(eventDetailCard(S.eventDetail));
}

function outcomePill(e) {
  const map = {
    called: ['pill-ok', '已外呼'], cancelled: ['pill-info', '人工取消'],
    suppressed: ['pill-info', '时段抑制'], no_call: ['pill-bad', '未外呼'],
    logged: ['pill-info', '仅记录'], open: ['pill-warn', '处置中'],
  };
  const [cls, text] = map[e.outcome] || ['pill-idle', e.outcome || '—'];
  return el('span', { class: `pill ${cls}`, text });
}

function eventRow(e) {
  const label = e.label;
  const isWarning = e.event_type === 'risk_warning';
  return el('tr', {}, [
    el('td', { class: 'mono', text: e.event_id }),
    el('td', { class: 'mono', text: shortTime(e.detected_at) }),
    el('td', {}, el('span', {
      class: `pill ${isWarning ? 'pill-info' : 'pill-idle'}`,
      text: typeName(e.event_type) })),
    el('td', {}, el('span', {
      class: `pill ${e.level === 'L3' || e.level === 'L4' ? 'pill-bad' : 'pill-warn'}`,
      text: e.level })),
    el('td', { class: 'mono', text: e.rule_id || '—' }),
    el('td', { text: e.trigger }),
    el('td', { class: 'num', text: String(e.confidence) }),
    el('td', {}, outcomePill(e)),
    el('td', { class: 'num',
      text: e.calls_ok ? `${e.calls_ok} 通${e.fallback_fired ? '（兜底）' : ''}` : '—' }),
    el('td', {}, label
      ? el('span', { class: `pill ${label.is_false_alarm ? 'pill-warn' : 'pill-ok'}`,
                     text: label.is_false_alarm ? '误报' : '真实' })
      : el('span', { class: 'pill pill-idle', text: '未标注' })),
    el('td', {}, el('div', { style: 'display:flex;gap:6px' }, [
      el('button', { class: 'btn btn-sm btn-ghost', text: '详情',
                     onclick: () => openEvent(e.event_id) }),
      el('button', { class: 'btn btn-sm btn-ghost', text: '误报',
                     onclick: () => labelEvent(e.event_id, true) }),
      el('button', { class: 'btn btn-sm btn-ghost', text: '真实',
                     onclick: () => labelEvent(e.event_id, false) }),
    ])),
  ]);
}

async function openEvent(id) {
  if (S.eventDetail && S.eventDetail.event_id === id) { S.eventDetail = null; render(); return; }
  const res = await api(`/api/events/${encodeURIComponent(id)}`);
  if (!res.ok) { toast(res.body.error || '读取失败', 'bad'); return; }
  S.eventDetail = res.body.event;
  render();
  const node = $('#event-detail');
  if (node && node.scrollIntoView) node.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function labelEvent(id, isFalse) {
  const reason = isFalse ? (prompt('误报原因（可留空）', '') ?? null) : '';
  if (reason === null) return;
  const res = await post(`/api/events/${encodeURIComponent(id)}/label`,
                         { is_false_alarm: isFalse, reason });
  if (!res.body.ok) { toast(res.body.reason || '标注失败', 'bad'); return; }
  toast(`已标注 ${id} 为${isFalse ? '误报' : '真实事件'}`, 'ok');
  await loadEvents();
  if (S.eventDetail && S.eventDetail.event_id === id) await openEvent(id);
}

function eventDetailCard(ev) {
  const evid = ev.event_id;
  const evd = ev.evidence || {};
  const shots = (evd.keyframes || []).slice().sort();
  const agent = ev.agent || {};
  const calls = agent.called || [];
  const grading = ev.grading || {};
  const ident = grading.identity || {};
  const mem = ev.memory || {};

  return el('div', { class: 'card', id: 'event-detail' }, [
    el('div', { style: 'display:flex;justify-content:space-between;align-items:center' }, [
      el('h2', { text: `事件详情 ${evid}` }),
      el('button', { class: 'btn btn-sm btn-ghost', text: '关闭',
                     onclick: () => { S.eventDetail = null; render(); } }),
    ]),
    el('dl', { class: 'kv', style: 'margin:12px 0 16px' }, [
      el('dt', { text: '时间' }), el('dd', { text: shortTime(ev.detected_at) }),
      el('dt', { text: '类型' }), el('dd', { text: typeName(ev.event_type) }),
      el('dt', { text: '地点' }), el('dd', { text: `${ev.room} · ${ev.resident}` }),
      ident.method ? el('dt', { text: '身份' }) : null,
      ident.method ? el('dd', {
        text: `${ident.resident_id || '未识别'}（${ident.detail || ident.method}`
              + `${ident.confidence ? `，置信度 ${ident.confidence}` : ''}）` }) : null,
      el('dt', { text: '判定' }),
      el('dd', { text: `${ev.trigger} · 置信度 ${ev.confidence}` }),
      // 分级理由是"为什么当时打了这个电话"的答案，必须显著
      el('dt', { text: '分级' }),
      el('dd', { text: `${ev.level} — 规则 ${grading.rule_id || '?'}：`
                       + `${grading.reason || ''}`
                       + (grading.escalated_by ? `（${grading.escalated_by}）` : '') }),
      mem.summary ? el('dt', { text: '住户记忆' }) : null,
      mem.summary ? el('dd', { text: mem.summary }) : null,
      el('dt', { text: '处置' }),
      el('dd', { text: `${ev.outcome}${agent.mode ? ` · ${agent.mode}` : ''}` +
                       `${ev.fallback_fired ? ' · 确定性兜底已触发' : ''}` }),
      agent.summary ? el('dt', { text: '摘要' }) : null,
      agent.summary ? el('dd', { text: agent.summary }) : null,
      agent.error ? el('dt', { text: '异常' }) : null,
      agent.error ? el('dd', { text: agent.error }) : null,
    ]),

    (grading.matched || []).length > 1 ? el('div', { style: 'margin-bottom:16px' }, [
      el('h2', { text: '命中的全部规则', style: 'margin-bottom:8px' }),
      el('p', { class: 'card-desc', style: 'margin:0 0 8px',
        text: '同一事件命中多条时取最高等级。全部列出以便回放决策链。' }),
      el('table', {}, [
        el('thead', {}, el('tr', {}, ['规则', '等级', '理由']
          .map((h) => el('th', { text: h })))),
        el('tbody', {}, grading.matched.map((m) => el('tr', {}, [
          el('td', { class: 'mono', text: m.rule_id }),
          el('td', { text: m.level }),
          el('td', { text: m.reason }),
        ]))),
      ]),
    ]) : null,

    grading.risk_at_detection ? el('div', { style: 'margin-bottom:16px' }, [
      el('h2', { text: '判定时的风险分', style: 'margin-bottom:8px' }),
      notice('info', `${grading.risk_at_detection.score} 分（`
        + `${grading.risk_at_detection.level}）　`
        + grading.risk_at_detection.summary),
    ]) : null,

    shots.length ? el('h2', { text: '关键帧', style: 'margin-bottom:10px' }) : null,
    shots.length ? el('div', { class: 'shots' }, shots.map((name) => el('figure', {}, [
      el('img', { src: apiPath(`/api/events/${encodeURIComponent(evid)}/evidence/${encodeURIComponent(name)}`),
                  loading: 'lazy', alt: name }),
      el('figcaption', { text: name.replace('keyframe_', '').replace('.jpg', '') }),
    ]))) : null,

    evd.clip ? el('div', { class: 'clip', style: 'margin-bottom:16px' }, [
      el('h2', { text: '现场短片', style: 'margin-bottom:10px' }),
      el('video', { controls: true, preload: 'metadata',
        src: apiPath(`/api/events/${encodeURIComponent(evid)}/evidence/${encodeURIComponent(evd.clip)}`) }),
    ]) : null,

    el('h2', { text: '判定特征', style: 'margin-bottom:8px' }),
    el('div', { class: 'feat', style: 'margin-bottom:16px' },
       Object.entries(ev.features || {}).map(([k, v]) => el('div', {}, [
         el('div', { class: 'k', text: k }),
         el('div', { class: 'v', text: String(v) }),
       ]))),

    calls.length ? el('h2', { text: '外呼结果', style: 'margin-bottom:8px' }) : null,
    calls.length ? el('table', { style: 'margin-bottom:16px' }, [
      el('thead', {}, el('tr', {}, ['联系人', '角色', '号码', '结果', '说明', '耗时']
        .map((h) => el('th', { text: h })))),
      el('tbody', {}, calls.map((c) => el('tr', {}, [
        el('td', { text: c.name }), el('td', { text: c.role }),
        el('td', { class: 'mono', text: c.phone }),
        el('td', {}, el('span', {
          class: `pill ${c.ok ? (c.dry_run ? 'pill-info' : 'pill-ok') : 'pill-bad'}`,
          text: c.code })),
        el('td', { text: c.message || '' }),
        el('td', { class: 'num', text: `${c.latency_ms}ms` }),
      ]))),
    ]) : null,

    el('h2', { text: '处置时间线', style: 'margin-bottom:8px' }),
    el('ul', { class: 'timeline', style: 'margin-bottom:16px' },
       (ev.timeline || []).map((t) => {
         const extra = Object.entries(t).filter(([k]) => k !== 'at' && k !== 'stage')
           .map(([k, v]) => `${k}=${v}`).join('  ');
         return el('li', {}, [
           el('span', { class: 'at', text: (t.at || '').slice(11, 19) }),
           el('span', { text: STAGE_LABEL[t.stage] || t.stage }),
           extra ? el('span', { class: 'extra', text: `　${extra}` }) : null,
         ]);
       })),

    el('h2', { text: '完整记录', style: 'margin-bottom:8px' }),
    el('div', { class: 'detail', text: JSON.stringify(ev, null, 2) }),
  ]);
}

/* ------------------------------------------------------------------ 配置 */
async function loadConfig() {
  const [sch, cfg] = await Promise.all([api('/api/schema'), api('/api/config')]);
  S.schema = sch.body;
  S.config = cfg.body;
  S.draft = JSON.parse(JSON.stringify(cfg.body.values || {}));
  S.warnings = cfg.body.warnings || [];
  S.errors = {};
  S.dirty = false;
  if (S.tab === 'config') render();
}

function draftSection(name) {
  const parts = name.split('.');
  let node = S.draft;
  for (const part of parts) {
    if (typeof node[part] !== 'object' || node[part] === null) {
      node[part] = parts[parts.length - 1] === part && part === 'contacts' ? [] : {};
    }
    node = node[part];
  }
  return node;
}

function getVal(sec, key) {
  const node = draftSection(sec.name);
  return Array.isArray(node) ? undefined : node[key];
}

function setVal(sec, key, value) {
  const node = draftSection(sec.name);
  node[key] = value;
  S.dirty = true;
  updateSaveBar();
}

function renderConfig(root) {
  if (!S.schema || !S.draft) {
    root.appendChild(el('div', { class: 'loading', text: '加载中…' }));
    return;
  }

  if (!S.config.saved) {
    root.appendChild(el('div', { class: 'card' }, [
      notice('info', `尚未创建 config.toml，表单里是 config.example.toml 的默认值。` +
                     `保存后会写入 ${S.config.path}。`),
    ]));
  }

  if (S.warnings.length) {
    root.appendChild(el('div', { class: 'card' }, [
      el('h2', { text: '提示' }),
      el('div', { style: 'margin-top:10px' }, S.warnings.map((w) => notice('warn', w))),
    ]));
  }

  for (const sec of S.schema.sections) {
    root.appendChild(sec.kind === 'rows' ? rowsSection(sec) : tableSection(sec));
  }

  root.appendChild(callTestCard());

  root.appendChild(el('div', { class: 'save-bar' }, [
    el('button', { class: 'btn btn-primary', id: 'btn-save', onclick: saveConfig,
                   text: '保存配置' }),
    el('button', { class: 'btn', onclick: validateConfig, text: '仅校验' }),
    el('button', { class: 'btn btn-ghost', onclick: () => loadConfig(), text: '放弃修改' }),
    el('div', { class: 'spacer' }),
    el('div', { class: 'msg', id: 'save-msg' }),
  ]));
  updateSaveBar();
}

function updateSaveBar() {
  const msg = $('#save-msg');
  if (!msg) return;
  const errCount = Object.keys(S.errors).length;
  if (errCount) {
    msg.textContent = `${errCount} 处填写有误，修正后才能保存`;
    msg.style.color = 'var(--red)';
  } else if (S.dirty) {
    msg.textContent = '有未保存的修改';
    msg.style.color = 'var(--amber)';
  } else {
    msg.textContent = `配置文件 ${S.config.path}`;
    msg.style.color = '';
  }
}

function tableSection(sec) {
  const card = el('div', { class: 'card' }, [
    el('h2', { text: sec.title }),
    sec.desc ? el('p', { class: 'card-desc', text: sec.desc }) : null,
  ]);
  card.appendChild(el('div', { class: 'fields' },
    sec.fields.map((f) => fieldNode(sec, f))));
  return card;
}

function fieldNode(sec, f) {
  const path = `${sec.name}.${f.key}`;
  const err = S.errors[path];
  const value = getVal(sec, f.key);
  const wrap = el('div', { class: `field${f.wide ? ' wide' : ''}` });

  if (f.type === 'bool') {
    wrap.appendChild(el('label', { class: 'switch' }, [
      el('input', {
        type: 'checkbox', checked: !!value,
        onchange: (e) => setVal(sec, f.key, e.target.checked),
      }),
      el('span', { class: 'track' }),
      el('span', { class: 'txt', text: f.label }),
    ]));
  } else if (f.type === 'zones') {
    wrap.appendChild(el('label', { text: f.label }));
    wrap.appendChild(zoneEditor(sec, f, Array.isArray(value) ? value : []));
  } else {
    wrap.appendChild(el('label', {}, [
      document.createTextNode(f.label),
      f.unit ? el('span', { class: 'unit', text: f.unit }) : null,
    ]));
    wrap.appendChild(inputFor(sec, f, value, err));
  }

  if (f.help) wrap.appendChild(el('div', { class: 'hint', text: f.help }));
  if (err) wrap.appendChild(el('div', { class: 'err', text: err }));
  return wrap;
}

function inputFor(sec, f, value, err) {
  const cls = err ? 'bad' : '';
  if (f.type === 'choice') {
    return el('select', {
      class: cls,
      onchange: (e) => setVal(sec, f.key, e.target.value),
    }, f.choices.map((c) => el('option', {
      value: c, text: c, selected: String(value) === c,
    })));
  }
  if (f.type === 'lines') {
    const text = Array.isArray(value) ? value.join('\n') : (value || '');
    return el('textarea', {
      class: cls, placeholder: f.placeholder, rows: Math.max(2, String(text).split('\n').length),
      onchange: (e) => setVal(sec, f.key,
        e.target.value.split('\n').map((s) => s.trim()).filter(Boolean)),
    }, text);
  }
  if (f.type === 'floats') {
    const text = Array.isArray(value) ? value.join(', ') : (value ?? '');
    return el('input', {
      type: 'text', class: cls, value: String(text), placeholder: f.placeholder,
      onchange: (e) => setVal(sec, f.key, e.target.value),
    });
  }
  if (f.type === 'int' || f.type === 'float') {
    return el('input', {
      type: 'number', class: cls, value: value ?? '',
      step: f.type === 'int' ? '1' : 'any',
      min: f.min ?? null, max: f.max ?? null, placeholder: f.placeholder,
      onchange: (e) => setVal(sec, f.key, e.target.value),
    });
  }
  return el('input', {
    type: 'text', class: cls, value: value ?? '', placeholder: f.placeholder,
    onchange: (e) => setVal(sec, f.key, e.target.value),
  });
}

function zoneEditor(sec, f, zones) {
  const box = el('div', { class: 'rows' });
  const repaint = () => { S.dirty = true; updateSaveBar(); render(); };

  zones.forEach((zone, idx) => {
    const row = el('div', { class: 'zone-row' });
    row.appendChild(el('span', { class: 'idx', text: `#${idx + 1}` }));
    ['x1', 'y1', 'x2', 'y2'].forEach((axis, i) => {
      row.appendChild(el('input', {
        type: 'number', step: '0.01', min: '0', max: '1',
        value: zone[i] ?? 0, title: axis,
        onchange: (e) => {
          zones[idx][i] = e.target.value;
          S.dirty = true;
          updateSaveBar();
        },
      }));
    });
    row.appendChild(el('button', {
      class: 'btn btn-sm btn-ghost', text: '删除',
      onclick: () => { zones.splice(idx, 1); setVal(sec, f.key, zones); repaint(); },
    }));
    box.appendChild(row);
  });

  if (!zones.length) {
    box.appendChild(el('div', { class: 'hint',
      text: '未设置。画面里有床铺等「躺倒属正常」的位置时，在这里框出来。' }));
  }
  box.appendChild(el('div', {}, el('button', {
    class: 'btn btn-sm', text: '+ 添加区域',
    onclick: () => {
      zones.push([0.0, 0.5, 1.0, 1.0]);
      setVal(sec, f.key, zones);
      repaint();
    },
  })));
  box.appendChild(el('div', { class: 'hint',
    text: '坐标是画面归一化值（左上 0,0 → 右下 1,1）。在「实时监控」页能看到蓝框标出的排除区域。' }));
  return box;
}

function rowsSection(sec) {
  const parent = sec.path.split('.');
  const leaf = parent.pop();
  let node = S.draft;
  for (const part of parent) {
    if (typeof node[part] !== 'object' || node[part] === null) node[part] = {};
    node = node[part];
  }
  if (!Array.isArray(node[leaf])) node[leaf] = [];
  const rows = node[leaf];

  const repaint = () => { S.dirty = true; updateSaveBar(); render(); };

  const card = el('div', { class: 'card' }, [
    el('h2', { text: sec.title }),
    sec.desc ? el('p', { class: 'card-desc', text: sec.desc }) : null,
  ]);

  const box = el('div', { class: 'rows' });
  rows.forEach((row, idx) => {
    const card2 = el('div', { class: 'row-card' });
    card2.appendChild(el('div', { class: 'row-order' }, [
      el('span', { text: `第 ${idx + 1} 位　${idx === 0 ? '（最先呼叫）' : ''}` }),
      el('span', {}, [
        el('button', { class: 'btn btn-sm btn-ghost', text: '↑', disabled: idx === 0,
          onclick: () => { rows.splice(idx - 1, 0, rows.splice(idx, 1)[0]); repaint(); } }),
        el('button', { class: 'btn btn-sm btn-ghost', text: '↓',
          disabled: idx === rows.length - 1,
          onclick: () => { rows.splice(idx + 1, 0, rows.splice(idx, 1)[0]); repaint(); } }),
      ]),
    ]));
    for (const f of sec.fields) {
      const path = `${sec.name}[${idx}].${f.key}`;
      const err = S.errors[path];
      const sub = el('div', { class: 'field' }, [
        el('label', { text: f.label }),
      ]);
      if (f.type === 'choice') {
        sub.appendChild(el('select', {
          class: err ? 'bad' : '',
          onchange: (e) => { row[f.key] = e.target.value; S.dirty = true; updateSaveBar(); },
        }, f.choices.map((c) => el('option', {
          value: c, text: c, selected: String(row[f.key]) === c }))));
      } else {
        sub.appendChild(el('input', {
          type: 'text', class: err ? 'bad' : '', value: row[f.key] ?? '',
          placeholder: f.placeholder,
          onchange: (e) => { row[f.key] = e.target.value; S.dirty = true; updateSaveBar(); },
        }));
      }
      if (err) sub.appendChild(el('div', { class: 'err', text: err }));
      card2.appendChild(sub);
    }
    card2.appendChild(el('div', { class: 'row-del' }, el('button', {
      class: 'btn btn-sm btn-danger', text: '删除',
      onclick: () => { rows.splice(idx, 1); repaint(); },
    })));
    box.appendChild(card2);
  });

  if (!rows.length) {
    box.appendChild(el('div', { class: 'empty',
      text: '还没有联系人。至少加一位 caregiver，否则判定成立后无人可呼。' }));
  }
  card.appendChild(box);
  card.appendChild(el('div', { style: 'margin-top:12px' }, el('button', {
    class: 'btn', text: '+ 添加联系人',
    onclick: () => {
      rows.push({ id: '', name: '', phone: '',
                  role: rows.length ? 'family' : 'caregiver' });
      repaint();
    },
  })));
  return card;
}

function collectErrors(list) {
  S.errors = {};
  for (const e of list || []) S.errors[e.field] = e.message;
}

async function validateConfig() {
  const res = await post('/api/config/validate', { values: S.draft });
  collectErrors(res.body.errors);
  S.warnings = res.body.warnings || [];
  render();
  if (res.body.ok) toast('校验通过，可以保存。', 'ok');
  else toast(`${res.body.errors.length} 处填写有误，已在表单里标出。`, 'bad');
}

async function saveConfig() {
  const btn = $('#btn-save');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  const res = await post('/api/config', { values: S.draft });
  collectErrors(res.body.errors);
  S.warnings = res.body.warnings || [];

  if (!res.body.ok) {
    render();
    toast(`保存失败：${res.body.errors.length} 处填写有误`, 'bad');
    return;
  }
  S.config.values = res.body.values;
  S.config.saved = true;
  S.draft = JSON.parse(JSON.stringify(res.body.values));
  S.dirty = false;
  render();
  toast(`已保存到 ${res.body.path}`, 'ok');
  if (res.body.note) toast(res.body.note, 'warn');
  for (const w of S.warnings) toast(w, 'warn');
  refreshStatus();
  S.doctor = null;
}

function callTestCard() {
  const contacts = ((S.draft.call || {}).contacts) || [];
  const result = S.callTest;
  return el('div', { class: 'card' }, [
    el('h2', { text: '外呼链路测试' }),
    el('p', { class: 'card-desc',
      text: '用当前**已保存**的配置发起一次测试。演练模式下只打印请求参数——注意演练一定返回成功，' +
            '它在校验密钥之前就返回了，所以演练通过不代表凭据没问题。' }),
    el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap' }, [
      el('select', { id: 'ct-contact', style: 'width:auto;min-width:180px' },
        contacts.length
          ? contacts.map((c) => el('option', { value: c.id,
              text: `${c.name || c.id}（${c.role}）` }))
          : [el('option', { value: '', text: '未配置联系人' })]),
      el('button', { class: 'btn', text: '演练测试', disabled: !contacts.length,
                     onclick: () => runCallTest(false) }),
      el('button', { class: 'btn btn-danger', text: '真实拨号（产生费用）',
                     disabled: !contacts.length, onclick: () => runCallTest(true) }),
    ]),
    result ? el('div', { style: 'margin-top:14px' }, [
      notice(result.result.ok ? (result.result.dry_run ? 'info' : 'ok') : 'bad',
             `${result.result.name}：code=${result.result.code} ${result.result.message}`),
      el('div', { class: 'detail', style: 'margin-top:10px',
        text: JSON.stringify({ 通道: result.describe, 播报槽位: result.slots,
                               请求参数: result.params, 结果: result.result }, null, 2) }),
    ]) : null,
  ]);
}

async function runCallTest(real) {
  if (real && !confirm('真实拨号会立即打出电话并产生费用，继续？')) return;
  const sel = $('#ct-contact');
  const res = await post('/api/call-test',
                         { real, contact_id: sel ? sel.value : '' });
  if (!res.body.ok) { toast(res.body.reason || '测试失败', 'bad'); return; }
  S.callTest = res.body;
  render();
  toast(res.body.result.ok ? '测试完成' : `失败：${res.body.result.code}`,
        res.body.result.ok ? 'ok' : 'bad');
}

/* ------------------------------------------------------------------ 体检 */
async function loadDoctor() {
  S.doctorLoading = true;
  if (S.tab === 'doctor') render();
  const res = await api('/api/doctor');
  S.doctor = res.body;
  S.doctorLoading = false;
  if (S.tab === 'doctor') render();
}

function renderDoctor(root) {
  if (!S.doctor || S.doctorLoading) {
    root.appendChild(el('div', { class: 'loading',
      text: S.doctorLoading ? '体检中（会尝试打开一次摄像头）…' : '加载中…' }));
    return;
  }
  const d = S.doctor;
  const sum = { ok: ['pill-ok', '全部通过'], warn: ['pill-warn', '有提示项'],
                fail: ['pill-bad', '有阻断项'] }[d.overall];

  root.appendChild(el('div', { class: 'card' }, [
    el('div', { style: 'display:flex;justify-content:space-between;align-items:center' }, [
      el('div', { style: 'display:flex;gap:10px;align-items:center' }, [
        el('span', { class: `pill ${sum[0]}`, text: sum[1] }),
        el('span', { class: 'card-desc', style: 'margin:0',
          text: `通过 ${d.counts.ok}　提示 ${d.counts.warn}　阻断 ${d.counts.fail}` +
                (d.counts.skip ? `　跳过 ${d.counts.skip}` : '') }),
      ]),
      el('button', { class: 'btn btn-sm', text: '重新体检', onclick: loadDoctor }),
    ]),
  ]));

  for (const g of d.groups) {
    const card = el('div', { class: 'card' }, [el('h2', { text: g.title })]);
    for (const it of g.items) {
      const pill = { ok: ['pill-ok', '通过'], warn: ['pill-warn', '提示'],
                     fail: ['pill-bad', '阻断'], skip: ['pill-idle', '跳过'] }[it.status];
      card.appendChild(el('div', { class: 'check' }, [
        el('div', {}, el('span', { class: `pill ${pill[0]}`, text: pill[1] })),
        el('div', { class: 'name', text: it.name }),
        el('div', {}, [
          el('div', { class: 'detail-text', text: it.detail }),
          it.hint ? el('div', { class: 'hint', text: it.hint }) : null,
        ]),
      ]));
    }
    root.appendChild(card);
  }
}

/* ------------------------------------------------------------------ 运行控制 */
async function startRun() {
  const num = (id) => {
    const node = $(id);
    return node && node.value !== '' ? Number(node.value) : null;
  };
  const checked = (id) => { const n = $(id); return !!(n && n.checked); };
  const body = {
    source: ($('#opt-source') || {}).value || '',
    backend: ($('#opt-backend') || {}).value || '',
    cancel_seconds: num('#opt-cancel'),
    max_seconds: num('#opt-max'),
    no_call: checked('#opt-nocall'),
    no_agent: checked('#opt-noagent'),
    quiet: checked('#opt-quiet'),
  };
  $('#btn-start').disabled = true;
  const res = await post('/api/run/start', body);
  if (!res.body.ok) {
    toast(res.body.reason || '启动失败', 'bad');
  } else {
    toast('已启动监测', 'ok');
    if (S.tab !== 'live') setTab('live');
  }
  await refreshStatus();
}

async function stopRun() {
  $('#btn-stop').disabled = true;
  $('#top-note').textContent = '正在停止，等待事件处置与留证收尾…';
  const res = await post('/api/run/stop', {});
  $('#top-note').textContent = '';
  toast(res.body.ok ? '已停止' : (res.body.reason || '停止超时'),
        res.body.ok ? 'ok' : 'warn');
  await refreshStatus();
  if (S.tab === 'events') loadEvents();
}

/* ------------------------------------------------------------------ 轮询 */
function applyStatusToChrome() {
  const st = S.status;
  const running = st && st.state === 'running';
  $('#btn-start').disabled = !!running;
  $('#btn-stop').disabled = !running;

  const pill = runStatePill(st);
  const holder = $('#side-state');
  holder.className = pill.className;
  holder.textContent = pill.textContent;

  if (st && st.config) {
    const c = st.config;
    $('#side-meta').textContent =
      `${c.place} · ${c.resident}\n${c.provider}/${c.dry_run ? '演练' : '真实'} · ` +
      `${c.contacts} 位联系人` + (running ? `\n${fmtDur(st.uptime)} · ${st.fps || 0}fps` : '');
  } else {
    $('#side-meta').textContent = st && st.config_error ? '配置异常' : '—';
  }

  if (st && st.error) $('#top-note').textContent = st.error;
}

let lastState = null;
let lastEventId = null;

async function refreshStatus() {
  const res = await api('/api/status');
  if (!res.ok) return;
  const prev = S.status;
  S.status = res.body;
  applyStatusToChrome();

  const st = S.status;
  const stateChanged = lastState !== st.state;
  lastState = st.state;

  // 新事件冒出来时提醒一下，并刷新事件页
  if (st.last_event_id && st.last_event_id !== lastEventId) {
    if (lastEventId !== null) toast(`检测到事件 ${st.last_event_id}`, 'bad');
    lastEventId = st.last_event_id;
    if (S.tab === 'events') loadEvents();
  }

  if (S.tab === 'live') {
    const running = st.state === 'running';
    const needRepaint = stateChanged
      || (!!prev && !!prev.active !== !!st.active)
      || (prev && prev.active && st.active &&
          (prev.active.banner !== st.active.banner
           || prev.active.event_id !== st.active.event_id
           || (prev.active.calls || []).length !== (st.active.calls || []).length));
    if (needRepaint) { render(); return; }
    // 只更新特征面板，不重建整页——重建会让 MJPEG 连接断开重连，画面闪。
    // 认准固定容器而不是内部的 .feat：启动瞬间 observation 还是空的，
    // 面板里只有占位符、没有 .feat，按 .feat 查会一直查不到，
    // 于是读数永远停在"运行后这里显示逐帧判定读数"（实测踩过）。
    const holder = $('#feat-panel');
    if (running && holder) {
      holder.replaceChildren(featurePanel(st.observation || {}, st.config));
    }
  }
  if (S.tab === 'overview') render();
}

// 事件在「后置证据窗口收满 → 合成 mp4 → 落盘」之后才出现在列表里，
// 比判定晚十几秒。停止运行后页面若不自己刷，用户会以为事件丢了。
async function refreshEventsIfStale() {
  if (S.tab !== 'events' || S.eventDetail) return;
  const before = S.events ? S.events.stats && S.events.stats.total : null;
  // 必须带上当前筛选条件，否则后台轮询会用全量结果覆盖掉用户选的类型，
  // 表现为"筛选几秒后自己变回全部"
  const q = S.eventType ? `&type=${encodeURIComponent(S.eventType)}` : '';
  const res = await api(`/api/events?limit=200${q}`);
  if (!res.ok) return;
  const after = res.body.stats ? res.body.stats.total : null;
  S.events = res.body;
  if (before !== after) render();
}

async function refreshLogs() {
  const res = await api(`/api/logs?since=${S.logSeq}`);
  if (!res.ok) return;
  const rows = res.body.logs || [];
  if (!rows.length) return;
  S.logs = S.logs.concat(rows).slice(-600);
  S.logSeq = res.body.latest;
  if (S.tab === 'live') paintLogs();
}

/* ------------------------------------------------------------------ 启动 */
function init() {
  if (API_BASE) $('#chat-link').hidden = false;
  for (const btn of document.querySelectorAll('#nav button')) {
    btn.addEventListener('click', () => {
      if (S.tab === 'config' && S.dirty && btn.dataset.tab !== 'config') {
        if (!confirm('配置有未保存的修改，离开会丢失。继续？')) return;
      }
      setTab(btn.dataset.tab);
    });
  }
  $('#btn-start').addEventListener('click', startRun);
  $('#btn-stop').addEventListener('click', stopRun);

  // 切到后台标签页时断开 MJPEG。浏览器在后台仍会持有连接并接收帧，
  // 而服务端每条连接都要推同一份数据——切几次窗口就会堆起好几条，
  // 把实时画面的 fps 拖垮。回到前台时重新渲染会自动重连。
  document.addEventListener('visibilitychange', () => {
    const img = $('#stream');
    if (!img) return;
    if (document.hidden) {
      img.dataset.paused = '1';
      img.removeAttribute('src');          // 断开连接，服务端随即释放席位
    } else if (img.dataset.paused) {
      delete img.dataset.paused;
      img.src = apiPath(`/api/stream.mjpg?t=${Date.now()}`);
    }
  });

  window.addEventListener('beforeunload', (e) => {
    if (S.dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  refreshStatus().then(render);
  refreshLogs();
  setInterval(refreshStatus, 1000);
  setInterval(refreshLogs, 1500);
  setInterval(refreshEventsIfStale, 4000);
}

init();
