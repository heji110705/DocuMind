// 装配层：把 api / ask / cites / docs / eval / session 接起来。
// 这里是唯一知道"页面上有哪些元素"的地方，其余模块只认自己需要的东西。
import { ApiError, apiKey, config, ready, setApiKey } from './api.js';
import { askQuestion } from './ask.js';
import { initCitations } from './cites.js';
import { initCorpus } from './docs.js';
import { loadEval, setRerank, speedHint } from './eval.js';
import { Session } from './session.js';

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);

const state = { busy: false, ctrl: null, rerank: 0, session: new Session(3), authRequired: false };

initCitations();

// ---------------------------------------------------------------- 速度开关
function paintSpeed() {
  $('speed').querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', +b.dataset.r === state.rerank);
  });
  setRerank(state.rerank);
}
$('speed').querySelectorAll('button').forEach(b => {
  b.onclick = () => { state.rerank = +b.dataset.r; paintSpeed(); };
});

// ---------------------------------------------------------------- 会话状态
function paintSession() {
  const n = state.session.size;
  $('sess').innerHTML = n
    ? `已带 <b>${n}</b> 轮上下文（指代会自动解析）`
    : '新会话（问第二句时自动带上这一轮的上下文）';
  $('reset-sess').hidden = n === 0;
}
$('reset-sess').onclick = () => { state.session.reset(); paintSession(); };

// ---------------------------------------------------------------- 提问
function setBusy(on) {
  state.busy = on;
  $('send').disabled = on;
  $('stop').hidden = !on;
}

$('stop').onclick = () => { if (state.ctrl) state.ctrl.abort(); };
$('form').onsubmit = e => { e.preventDefault(); ask(); };

document.querySelectorAll('#samples span').forEach(s => {
  s.onclick = () => { $('q').value = s.textContent; ask(); };
});

async function ask() {
  const question = $('q').value.trim();
  if (!question || state.busy) return;
  $('empty').style.display = 'none';
  setBusy(true);
  $('q').blur();

  state.ctrl = new AbortController();
  try {
    const history = state.session.payload();
    const out = await askQuestion({
      question,
      rerank: state.rerank,
      history,
      log: $('log'),
      signal: state.ctrl.signal,
    });
    if (!out.refused) {
      state.session.add(question, out.answer);
      paintSession();
    }
  } finally {
    state.ctrl = null;
    setBusy(false);
    $('q').value = '';
  }
}

// ---------------------------------------------------------------- 能力探测
async function boot() {
  if (params.get('rerank') === '1') state.rerank = 1;
  paintSpeed();
  paintSession();

  let cfg = { auth_required: false, allow_upload: true, index: 'prod' };
  try {
    cfg = await config();
  } catch (_) { /* 配置接口挂了也要能提问 */ }
  state.authRequired = !!cfg.auth_required;

  // 只有在服务端配置了 API_KEYS 时才显示 Key 输入框。
  // 本地演示（开放模式）不该出现一个"密码框"——那会让人以为这里有安全机制。
  $('keybox').hidden = !state.authRequired;
  if (params.get('key')) setApiKey(params.get('key'));
  if ($('keyin')) $('keyin').value = apiKey();
  $('key-save').onclick = async () => {
    setApiKey($('keyin').value.trim());
    await refreshReady();
    loadEval().catch(() => {});
  };

  if (!cfg.allow_upload) $('corpus').hidden = true;
  else $('corpus').hidden = false;
  initCorpus({
    onIngested: () => {
      refreshReady();
      // 回答完新文档就能问了，把列表也刷一下
      $('q').placeholder = '问点关于刚上传的文档的，比如它里面的关键数字';
    },
  });

  await refreshReady();
  loadEval().then(d => {
    if (!d) return;
    const h = speedHint(d.rows);
    if (h) {
      $('why').innerHTML =
        `快速 = 图谱+向量召回 + 证据判定，可答率 <b>${h.fast}%</b>（p50 ${(h.fastMs / 1000).toFixed(1)}s）；`
        + `高精度 = 再加本地 cross-encoder 重排，可答率 <b>${h.precise}%</b>`
        + `（p50 ${(h.preciseMs / 1000).toFixed(1)}s，慢在笔记本功耗墙，详见 README 第 14 节）。`;
    }
    if (params.get('panel') === '1') $('evalbox').open = true;
  }).catch(() => {});

  if (params.get('q')) {
    $('q').value = params.get('q');
    setTimeout(ask, 60);
  }
}

async function refreshReady() {
  try {
    const r = await ready();
    const tenant = r.tenant && r.tenant !== 'default' ? ` · 租户 <b>${r.tenant}</b>` : '';
    $('meta').innerHTML =
      `语料 <b>${r.documents}</b> 篇 / <b>${r.chunks}</b> 块 / <b>${(r.chars || 0).toLocaleString()}</b> 字`
      + `${tenant}。答案里的 [n] 可点，右侧会展开原文段落；证据不够时它会直接说不知道。`;
    $('badge').textContent = `索引 ${r.index}`;
    document.title = `DocuMind · ${r.chunks} 块`;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      $('keybox').hidden = false;
      $('meta').innerHTML = '<span style="color:#a33">这个实例要求 API Key，先在上面填入再提问。</span>';
      return;
    }
    $('meta').textContent = `索引状态读取失败：${e.message}`;
  }
}

boot();
