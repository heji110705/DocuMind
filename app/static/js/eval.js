// 把评测表搬进界面：数字只有一份，存在 data/eval_prod.json 里，
// 界面上的准确率由 /v1/eval 注入——省得以后改了一处忘了另一处。
import { evalRows } from './api.js';

const LABEL = {
  naive: '纯向量',
  mix: '+图谱',
  mix_guard: '+证据判定',
  mix_rr: '+重排',
  full: '+重排+判定',
};

let ROWS = [];
let TOTAL = 0;
let rerank = 0;

export function setRerank(v) {
  rerank = v;
  paint();
}

export function paint() {
  if (!ROWS.length) return;
  const cur = rerank ? 'full' : 'mix_guard';
  document.getElementById('evaltbl').innerHTML =
    '<tr><th>配置</th><th>可答率</th><th>拒答正确</th><th>总体</th><th>p50</th></tr>'
    + ROWS.map(x =>
      `<tr class="${x.config === cur ? 'hl' : ''}"><td>${LABEL[x.config] || x.config}</td>`
      + `<td>${x.acc_fact}%</td><td>${x.refusal_acc}%</td><td>${x.overall}%</td>`
      + `<td>${(x.p50_ms / 1000).toFixed(1)}s</td></tr>`).join('');
}

export async function loadEval() {
  const d = await evalRows();
  if (!d.available || !(d.rows || []).length) return null;
  ROWS = d.rows;
  TOTAL = d.n;
  paint();
  document.getElementById('evalbox').hidden = false;
  document.getElementById('evalnote').textContent =
    `${TOTAL} 题（v2 金标：94 可答 + ${TOTAL - 94} 必拒）。可答判定用关键词命中，不用模型自评；`
    + 'p50 是并发跑批时互相抢 CPU 的数字，单请求会好一些。'
    + `另有一套 316 题扩题（可答 256）的结果在 README 2.2 节：`
    + '同一套系统在扩题上的可答率是 66.8%，掉的原因已归因到"答案只在一两个块里"，不是金标错。';
  return d;
}

// 快速/高精度两档各自的准确率，用同一份数据回答"我该选哪个"
export function speedHint(rows) {
  const acc = {};
  (rows || []).forEach(x => { acc[x.config] = x; });
  if (!acc.mix_guard || !acc.full) return null;
  return {
    fast: acc.mix_guard.acc_fact,
    precise: acc.full.acc_fact,
    fastMs: acc.mix_guard.p50_ms,
    preciseMs: acc.full.p50_ms,
  };
}
