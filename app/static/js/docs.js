// 语料管理：上传 + 清单。
//
// 这个面板存在的理由不是"加个上传按钮"，而是让"增量索引"这件事可见：
// 上传前后语料规模变了、块数变了、刚才问不出来的问题现在能答了。
import { documents, removeDocument, upload } from './api.js';

export function initCorpus({ onIngested }) {
  const zone = document.getElementById('drop');
  const fileInput = document.getElementById('file');
  const msg = document.getElementById('up-msg');
  const tbody = document.getElementById('doc-rows');
  const refreshBtn = document.getElementById('refresh-docs');
  if (!zone) return null;

  function say(html) { msg.innerHTML = html; }

  function paint(items) {
    if (!tbody) return;
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">语料为空</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(d => `<tr>
      <td>${escapeHtml(d.file_path)}</td>
      <td class="n">${(d.chars || 0).toLocaleString()}</td>
      <td class="n">${d.chunks || 0}</td>
      <td class="n"><span class="del" data-id="${escapeHtml(d.doc_id)}" title="删除这篇">×</span></td>
    </tr>`).join('');
    tbody.querySelectorAll('.del').forEach(el => {
      el.onclick = () => removeDoc(el.dataset.id, el.closest('tr').firstElementChild.textContent);
    });
  }

  async function removeDoc(docId, name) {
    say(`<span>正在删除 ${escapeHtml(name)} …</span>`);
    try {
      const r = await removeDocument(docId);
      say(`<span class="ok">✓ 已删除 ${escapeHtml(name)}：-${r.chunks_removed} 块（剩 ${r.chunks_total} 块）</span>`);
      await refresh();
      if (onIngested) onIngested(r);
    } catch (e) {
      say(`<span class="err">✗ 删除失败：${escapeHtml(e.message)}</span>`);
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  async function refresh() {
    try {
      const d = await documents();
      paint(d.items || []);
    } catch (e) {
      paint([]);
      say(`<span class="err">读不到语料清单：${escapeHtml(e.message)}</span>`);
    }
  }

  async function send(file) {
    if (!file) return;
    say(`<span>正在入库 ${escapeHtml(file.name)} …（解析 → 抽实体 → 建向量，首次会慢一些）</span>`);
    const t0 = performance.now();
    try {
      const r = await upload(file);
      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      const dup = r.duplicate
        ? '\n内容与已有文档相同，已去重（块数未变）。'
        : '';
      say(`<span class="ok">✓ ${escapeHtml(r.file_path)}：${r.chars.toLocaleString()} 字 → `
        + `+${r.chunks_added} 块（共 ${r.chunks_total} 块），${secs}s，¥${Number(r.cost_cny).toFixed(4)}</span>`
        + (r.notes && r.notes.length ? `\n<span>${r.notes.map(escapeHtml).join('\n')}</span>` : '')
        + dup);
      await refresh();
      if (onIngested) onIngested(r);
    } catch (e) {
      say(`<span class="err">✗ 入库失败：${escapeHtml(e.message)}`
        + (e.code ? `（${e.code}）` : '') + '</span>');
    }
  }

  fileInput.onchange = () => { send(fileInput.files[0]); fileInput.value = ''; };
  if (refreshBtn) refreshBtn.onclick = refresh;

  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.remove('hot');
  }));
  zone.addEventListener('drop', e => {
    const f = e.dataTransfer.files[0];
    if (f) send(f);
  });

  refresh();
  return { refresh };
}
