// 文本 → DOM。只处理三样东西：转义、粗体、引用角标。
// 不引 markdown 库：模型输出是"结论 + 依据 + [n]"，其余记号越少越安全。

export function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

export function renderAnswer(text) {
  const h = esc(text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  return h.replace(/\[(\d{1,2})\]/g, (_, n) => `<span class="cite" data-n="${n}">${n}</span>`);
}

// 抽屉里显示的是 chunk 原文，带 markdown 记号。只去掉影响阅读的记号，
// 不做完整渲染——原文本来就是给人对照的，越接近原样越好。
export function renderQuote(text) {
  return esc(text)
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/^\s*[-*]\s+/gm, '· ');
}
