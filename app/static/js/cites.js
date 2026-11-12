// 引用抽屉。
//
// 引用按**卡片**存：以前是一个全局数组，问第二个问题时会把第一个问题的引用覆盖掉，
// 于是点老卡片上的 [2] 要么没反应，要么展开成另一个问题的原文。
// WeakMap 的键是卡片 DOM，卡片被移除时引用自动回收，不需要手动清理。
import { renderQuote } from './render.js';

const STORE = new WeakMap();

export function bindCitations(card) {
  const holder = { items: [] };
  STORE.set(card, holder);
  return holder;
}

export function openCitation(card, n) {
  const holder = card && STORE.get(card);
  const item = holder && holder.items.find(x => x.n === n);
  if (!item) return;
  document.getElementById('d-src').textContent = `[${n}] ${item.file_path}`;
  document.getElementById('d-body').innerHTML = renderQuote(item.snippet);
  document.getElementById('drawer').classList.add('on');
}

export function closeCitation() {
  document.getElementById('drawer').classList.remove('on');
}

export function initCitations() {
  document.getElementById('close').onclick = closeCitation;
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCitation(); });
  const drawer = document.getElementById('drawer');
  drawer.addEventListener('click', e => { if (e.target === drawer) closeCitation(); });
  document.addEventListener('click', e => {
    const el = e.target.closest('.cite');
    if (!el) return;
    openCitation(el.closest('.card'), +el.dataset.n);
  });
}
