// 一次提问的完整生命周期：建卡片 → 消费 SSE → 更新阶段/答案/脚注。
import { askStream } from './api.js';
import { esc, renderAnswer } from './render.js';
import { bindCitations } from './cites.js';

const STAGE_LABEL = {
  rewrite: '解析上下文',
  retrieve: '检索证据',
  judge: '核对证据',
  answer: '生成答案',
};

function footBits(d) {
  const bits = [];
  if (d.elapsed_ms != null) bits.push(`${(d.elapsed_ms / 1000).toFixed(1)}s`);
  if (d.tokens_in) bits.push(`in ${d.tokens_in}`);
  if (d.tokens_out) bits.push(`out ${d.tokens_out}`);
  if (d.cost_cny != null) bits.push(`¥${Number(d.cost_cny).toFixed(4)}`);
  if (d.refused) bits.push('已拒答');
  return bits;
}

export async function askQuestion({ question, rerank, history, log, signal }) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<p class="q">${esc(question)}</p>
    <div class="reuse" hidden></div>
    <div class="stages">${Object.entries(STAGE_LABEL)
      .map(([k, v]) => `<i data-k="${k}">${v}</i>`).join('')}<span class="elapsed"></span></div>
    <div class="ans"></div>
    <div class="foot"></div>`;
  log.prepend(card);

  const ans = card.querySelector('.ans');
  const foot = card.querySelector('.foot');
  const reuse = card.querySelector('.reuse');
  const elapsed = card.querySelector('.elapsed');
  const holder = bindCitations(card);

  let text = '';
  let done = false;
  const t0 = performance.now();
  const timer = setInterval(() => {
    elapsed.textContent = ((performance.now() - t0) / 1000).toFixed(0) + 's';
  }, 250);

  try {
    for await (const { event, data } of askStream(
      { question, mode: 'mix', enable_rerank: !!rerank, history },
      signal,
    )) {
      if (event === 'stage') {
        const el = card.querySelector(`i[data-k="${data.name}"]`);
        if (!el) continue;
        if (data.status === 'start') {
          // rewrite 阶段在没有历史时是空跑，直接隐藏，别让人以为每次都多一次模型调用
          if (data.skip) el.classList.add('skip');
          else el.classList.add('on');
        } else {
          el.classList.remove('on');
          if (!el.classList.contains('skip')) el.classList.add('done');
        }
      } else if (event === 'meta') {
        // 多轮时把"这一轮到底按什么问的"显示出来：指代消解是黑盒，
        // 不给用户看改写结果的话，答错时无法判断是检索错了还是理解错了。
        if (data.history_used > 0 && data.standalone && data.standalone !== question) {
          reuse.hidden = false;
          reuse.innerHTML = `本轮按 <b>${esc(data.standalone)}</b> 检索（已代入上文）`;
        }
      } else if (event === 'token') {
        text += data.text;
        ans.innerHTML = renderAnswer(text);
        ans.classList.toggle('refused', text.startsWith('证据不足'));
      } else if (event === 'citations') {
        holder.items = data.items || [];
      } else if (event === 'error') {
        done = true;
        ans.innerHTML = `<span class="refused">出错了：${esc(data.message)}</span>`;
      } else if (event === 'done') {
        done = true;
        foot.innerHTML = footBits(data).map(b => `<span>${b}</span>`).join('');
      }
    }
    if (!done && !text) ans.innerHTML = '<span class="refused">连接中断，没有收到答案</span>';
  } catch (e) {
    if (e.name === 'AbortError') {
      foot.innerHTML = '<span>已停止</span>';
      if (!text) ans.innerHTML = '<span class="refused">已停止</span>';
    } else {
      ans.innerHTML = `<span class="refused">出错了：${esc(e.message)}</span>`;
    }
  } finally {
    clearInterval(timer);
    elapsed.remove();
  }

  return { answer: text, refused: text.startsWith('证据不足') };
}
